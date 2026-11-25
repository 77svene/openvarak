"""Loopback webhook daemon with durable jobs and one active GPU agent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from openvarak.engine.desktop import build_desktop_engine
from openvarak.runtime.actions import (
    ActionBlocked,
    ActionJournal,
    action_deadline,
    action_job,
)
from openvarak.runtime.lease import RuntimeLease
from openvarak.runtime.verification import verify
from openvarak.tools.storage.persistent import HybridPersistentMemoryBackend

logger = logging.getLogger(__name__)


class DesktopSessionStore:
    def __init__(self, memory):
        self.memory = memory

    def get_or_create(self, session_id):
        state = self.memory.latest_checkpoint("session:" + session_id) or {
            "messages": []
        }
        return SimpleNamespace(messages=state["messages"])

    def save_message(self, session_id, message):
        messages = self.get_or_create(session_id).messages
        self.memory.checkpoint(
            "session:" + session_id, {"messages": [*messages, message][-20:]}
        )


class DesktopRuntime:
    """One serialized agent, persistent browser, memory, and skill manager.

    Inference and tools run off the event loop, so webhook acceptance remains
    responsive during long turns. Pending jobs survive restart. A crash during
    a job causes at-least-once replay; callers should make side effects idempotent.
    """

    def __init__(self, config, *, agent=None, memory=None, engine=None):
        self.config = config
        self.memory = memory or HybridPersistentMemoryBackend.from_config(config)
        self.engine = engine or build_desktop_engine(config)
        self._agent = agent
        self._optimizer = None
        self._owned_tools = []
        self._queue = asyncio.Queue(maxsize=config.desktop.queue_size)
        self._worker = None
        self._stopping = False
        self._closed = False
        self._temporal = None
        self._job_lock = threading.RLock()
        self._agent_lock = threading.Lock()
        self._db = sqlite3.connect(self.memory._db_path, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""CREATE TABLE IF NOT EXISTS varak_jobs (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, created_at REAL NOT NULL)""")
        self._db.commit()
        self._db.execute("""CREATE TABLE IF NOT EXISTS varak_job_keys (
            source TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            job_id TEXT NOT NULL, PRIMARY KEY(source,event_id))""")
        self._db.commit()
        self._lease = RuntimeLease(self.memory._db_path + ".runtime.lock")
        self.workspace = Path(
            config.desktop.workspace_root
            or Path(self.memory._db_path).parent / "workspace"
        ).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        target = self.memory._db_path
        if config.desktop.action_backend == "postgres":
            target = os.environ.get(config.desktop.action_dsn_env, "")
            if not target:
                raise ValueError(
                    f"Set {config.desktop.action_dsn_env} for PostgreSQL actions"
                )
        self.actions = ActionJournal(
            target,
            backend=config.desktop.action_backend,
            budget_minor=config.desktop.action_budget_minor,
            currency=config.desktop.action_currency,
        )

    def _build_agent(self):
        from openvarak.agents.monitor_operative import MonitorOperativeAgent
        from openvarak.agents.native_openhands import NativeOpenHandsAgent
        from openvarak.agents.operative import OperativeAgent
        from openvarak.agents.rlm import RLMAgent
        from openvarak.learning.agents.live_skill_optimizer import LiveSkillOptimizer
        from openvarak.sandbox.skills import SkillSandbox
        from openvarak.skills.synthesis import SkillSynthesizer, SynthesizeSkillTool
        from openvarak.tools.playwright_session import PlaywrightSessionTool
        from openvarak.tools.storage_tools import MemoryRetrieveTool, MemoryStoreTool

        browser = PlaywrightSessionTool()
        self._owned_tools.append(browser)
        tools = [
            browser,
            MemoryRetrieveTool(backend=self.memory),
            MemoryStoreTool(backend=self.memory),
        ]
        synthesizer = SkillSynthesizer(
            sandbox=SkillSandbox(image=self.config.desktop.skill_image)
        )
        if self.config.desktop.skills_enabled:
            tools.extend(synthesizer.load_saved())
            tools.append(SynthesizeSkillTool(synthesizer))
        # Resolve the user's configured execution/search/file tools through the
        # existing registry; these keep the same capability checks and schemas.
        from openvarak.agents.tool_resolver import resolve_agent_tools

        resolved = resolve_agent_tools(
            {"config": {"tools": self.config.tools.enabled}},
            engine=self.engine,
            model=self.config.intelligence.default_model
            or self.config.desktop.local_model,
            memory_backend=self.memory,
            app_config=self.config,
        )
        existing = {tool.spec.name for tool in tools}
        tools.extend(
            tool for tool in resolved.instances if tool.spec.name not in existing
        )
        self._owned_tools.append(resolved)
        kinds = {
            "operative": OperativeAgent,
            "monitor_operative": MonitorOperativeAgent,
            "native_openhands": NativeOpenHandsAgent,
            "rlm": RLMAgent,
        }
        kind = self.config.desktop.agent_type
        if kind == "pydantic":
            import sys

            if sys.version_info < (3, 11):
                raise ValueError("The durable/Pydantic extra requires Python 3.11+")
            from openvarak.agents.pydantic_desktop import PydanticDesktopAgent

            agent = PydanticDesktopAgent(self.config, tools=tools)
            agent._executor.configure_journal(self.actions)
            return agent
        if kind not in kinds:
            raise ValueError(f"Unsupported desktop agent_type: {kind}")
        kwargs = {
            "tools": tools,
            "max_turns": self.config.agent.max_turns,
            "max_tokens": self.config.intelligence.max_tokens,
        }
        if kind in {"operative", "monitor_operative"}:
            kwargs.update(
                memory_backend=self.memory,
                operator_id="desktop",
                session_store=DesktopSessionStore(self.memory),
            )
        agent = kinds[kind](
            self.engine,
            self.config.intelligence.default_model or self.config.desktop.local_model,
            **kwargs,
        )
        agent._executor.configure_journal(self.actions)
        if self.config.desktop.skills_enabled:
            self._optimizer = LiveSkillOptimizer(
                self.engine, agent._model, synthesizer, self.memory
            )
            agent._executor.configure_repair(
                self._optimizer,
                self.config.desktop.self_heal_threshold,
            )
        return agent

    async def start(self):
        self._lease.acquire()
        if self._agent is None:
            self._agent = await asyncio.to_thread(self._build_agent)
        else:
            from openvarak.tools._stubs import ToolExecutor

            executor = getattr(self._agent, "_executor", None)
            if isinstance(executor, ToolExecutor):
                executor.configure_journal(self.actions)
        # Process death releases the lease. Interrupted jobs are quarantined,
        # never blindly replayed across an unknown external side effect.
        with self._job_lock, self._db:
            interrupted = [
                r[0]
                for r in self._db.execute(
                    "SELECT id FROM varak_jobs WHERE status='running'"
                ).fetchall()
            ]
            self._db.execute(
                "UPDATE varak_jobs SET status='needs_reconciliation' "
                "WHERE status='running'"
            )
            rows = self._db.execute(
                "SELECT id FROM varak_jobs WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        self.actions.recover(interrupted)
        if self.config.desktop.orchestrator == "temporal":
            import sys

            if sys.version_info < (3, 11):
                raise ValueError("The durable/Temporal extra requires Python 3.11+")
            from openvarak.runtime.temporal import TemporalDispatcher

            self._temporal = TemporalDispatcher(self, self.config.desktop)
            await self._temporal.start()
        elif self.config.desktop.orchestrator != "sqlite":
            raise ValueError("orchestrator must be sqlite or temporal")
        self._worker = asyncio.create_task(self._consume())
        for (job_id,) in rows:
            await self._queue.put(job_id)

    def submit(self, payload):
        if self._stopping:
            raise asyncio.QueueFull
        if not isinstance(payload, dict) or not isinstance(payload.get("prompt"), str):
            raise ValueError("Expected JSON object with a prompt string")
        if not payload["prompt"].strip() or len(payload["prompt"]) > 100000:
            raise ValueError("Prompt must contain 1–100000 characters")
        import math

        from openvarak.runtime.verification import validate_contract

        validate_contract(payload.get("acceptance"), self.workspace)
        deadline = payload.get("expires_at")
        if deadline is not None and (
            type(deadline) not in {int, float} or not math.isfinite(deadline)
        ):
            raise ValueError("expires_at must be a finite Unix timestamp")
        import hashlib

        raw = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        source = payload.get("source", "webhook")
        event_id = payload.get("event_id")
        if not isinstance(source, str) or not source or len(source) > 192:
            raise ValueError("source must contain 1–192 characters")
        if event_id is not None and (
            not isinstance(event_id, str) or not event_id or len(event_id) > 192
        ):
            raise ValueError("event_id must contain 1–192 characters")
        digest = hashlib.sha256(raw.encode()).hexdigest()
        job_id = uuid.uuid4().hex
        with self._job_lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if event_id is not None:
                old = self._db.execute(
                    "SELECT payload_hash,job_id FROM varak_job_keys "
                    "WHERE source=? AND event_id=?",
                    (source, event_id),
                ).fetchone()
                if old:
                    if old[0] != digest:
                        raise ValueError(
                            "event_id already exists with a different payload"
                        )
                    return old[1]
            if self._queue.full():
                raise asyncio.QueueFull
            self._db.execute(
                "INSERT INTO varak_jobs VALUES (?,?,'pending',NULL,?)",
                (job_id, raw, time.time()),
            )
            if event_id is not None:
                self._db.execute(
                    "INSERT INTO varak_job_keys VALUES (?,?,?,?)",
                    (source, event_id, digest, job_id),
                )
            self._queue.put_nowait(job_id)
        return job_id

    def job(self, job_id):
        with self._job_lock:
            row = self._db.execute(
                "SELECT status,result FROM varak_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return (
            {
                "id": job_id,
                "status": row[0],
                "result": json.loads(row[1]) if row[1] else None,
            }
            if row
            else None
        )

    def _status(self, job_id, status, result=None):
        with self._job_lock, self._db:
            self._db.execute(
                "UPDATE varak_jobs SET status=?,result=? WHERE id=?",
                (status, json.dumps(result), job_id),
            )

    async def _consume(self):
        while not self._stopping:
            job_id = await self._queue.get()
            try:
                if job_id is None:
                    return
                try:
                    if self._temporal:
                        await self._temporal.dispatch(job_id)
                    else:
                        await self._execute_job(job_id)
                except Exception as exc:
                    logger.warning(
                        "Desktop job %s failed: %s", job_id, type(exc).__name__
                    )
                    # A transport failure is not proof that the remote activity
                    # did not run. Preserve authoritative state for recovery.
                    if self._temporal is None:
                        self._status(job_id, "failed", {"error": str(exc)})
            finally:
                self._queue.task_done()

    async def _execute_job(self, job_id):
        with self._job_lock, self._db:
            row = self._db.execute(
                "SELECT payload,status FROM varak_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Job is absent from this runtime's state directory")
            claimed = self._db.execute(
                "UPDATE varak_jobs SET status='running' "
                "WHERE id=? AND status='pending'",
                (job_id,),
            ).rowcount
            if not claimed:
                return self.job(job_id)
        payload = json.loads(row[0])
        if (
            payload.get("expires_at") is not None
            and time.time() >= payload["expires_at"]
        ):
            self._status(job_id, "expired", {"error": "Job deadline expired"})
            return self.job(job_id)
        try:
            execution = asyncio.create_task(
                asyncio.to_thread(self._run, payload, job_id)
            )
            try:
                trace = await asyncio.shield(execution)
            except asyncio.CancelledError:
                self._status(
                    job_id,
                    "needs_reconciliation",
                    {"error": "Execution interrupted; tool threads may still finish"},
                )
                # Python cannot kill the model/tool thread. Keep this activity
                # occupied until it stops, rather than admitting a second GPU job.
                try:
                    await execution
                except Exception:
                    pass
                raise
            if self.actions.unresolved(job_id):
                self._status(job_id, "needs_reconciliation", trace)
            else:
                trace["verification"] = verify(
                    payload.get("acceptance"), trace, self.workspace
                )
                state = trace["verification"]["state"]
                status = (
                    "completed"
                    if state == "verified"
                    else "verification_failed"
                    if state == "failed"
                    else "unverified"
                )
                if (
                    state == "unverified"
                    and not self.config.desktop.require_verified_completion
                ):
                    status = "completed"
                self.memory.checkpoint("desktop", trace)
                self._status(job_id, status, trace)
        except Exception as exc:
            status = (
                "needs_reconciliation" if self.actions.actions(job_id) else "failed"
            )
            self._status(job_id, status, {"error": str(exc)})
        return self.job(job_id)

    def reconcile_action(self, action_id):
        executor = getattr(self._agent, "_executor", None)

        def lookup(name, params):
            tool = executor._tools.get(name) if executor is not None else None
            reconcile = getattr(tool, "reconcile_action", None)
            if not callable(reconcile):
                raise ActionBlocked(
                    "This tool has no authoritative reconciliation adapter"
                )
            return reconcile(**params)

        return self.actions.reconcile(action_id, lookup)

    def resume(self, job_id):
        rows = self.actions.actions(job_id)
        if not rows or self.actions.unresolved(job_id):
            raise ActionBlocked(
                "Recorded actions must be reconciled before explicit resume"
            )
        with self._job_lock:
            current = self.job(job_id)
            if not current or current["status"] != "needs_reconciliation":
                raise ValueError("Only a quarantined job can be resumed")
            if self._queue.full():
                raise asyncio.QueueFull
            self._status(job_id, "pending")
            self._queue.put_nowait(job_id)

    def _run(self, payload, job_id):
        with self._agent_lock:
            return self._run_serialized(payload, job_id)

    def _run_serialized(self, payload, job_id):
        from openvarak.engine.desktop import routing_hints

        failures = []
        threshold = max(1, self.config.desktop.self_heal_threshold)
        prompt = payload["prompt"]
        # N attempts followed by at most one optimizer-assisted attempt.
        attempts = threshold + 1 if self.config.desktop.retry_tasks else 1
        for attempt in range(attempts):
            job_token = action_job.set(job_id)
            deadline_token = action_deadline.set(payload.get("expires_at"))
            token = routing_hints.set(
                {
                    "high_depth_reasoning": payload.get("high_depth_reasoning") is True,
                    "retry_count": attempt,
                }
            )
            try:
                if attempt > 0 and attempt == threshold:
                    if self._optimizer is None:
                        raise RuntimeError("Task failed repeatedly: " + failures[-1])
                    prompt = self._optimizer.recover_task(prompt, failures)
                result = self._agent.run(prompt)
                if result.metadata.get("error") or result.metadata.get(
                    "max_turns_exceeded"
                ):
                    # Policy denials are final, not candidates for optimizer bypass.
                    if result.metadata.get("security_denied"):
                        raise PermissionError(result.content)
                    raise RuntimeError(
                        result.content or "Agent did not complete the task"
                    )
                trace = {
                    "job_id": job_id,
                    "content": result.content,
                    "turns": result.turns,
                    "retries": attempt,
                    "tool_results": [
                        {
                            "tool": t.tool_name,
                            "success": t.success,
                            "content": t.content,
                        }
                        for t in result.tool_results
                    ],
                }
                if isinstance(result.metadata.get("typed_output"), dict):
                    trace["typed_output"] = result.metadata["typed_output"]
                self.memory.checkpoint("desktop", trace, kind="trace")
                return trace
            except PermissionError:
                raise
            except Exception as exc:
                failures.append(str(exc))
                self.memory.checkpoint(
                    job_id, {"attempt": attempt, "error": str(exc)}, kind="failed_trace"
                )
                if attempt == attempts - 1 or self.actions.actions(job_id):
                    raise
            finally:
                routing_hints.reset(token)
                action_job.reset(job_token)
                action_deadline.reset(deadline_token)

    async def close(self):
        if self._closed:
            return
        self._stopping = True
        if self._worker:
            if not self._worker.done() and self._queue.empty():
                self._queue.put_nowait(None)
            await self._worker  # finish active job; pending work stays durable
        if self._temporal:
            await self._temporal.close()
        for tool in reversed(self._owned_tools):
            await asyncio.to_thread(tool.close)
        self.engine.close()
        self._db.close()
        self.actions.close()
        await asyncio.to_thread(self.memory.close)
        self._lease.close()
        self._closed = True


def create_app(config, *, runtime=None):
    from fastapi import FastAPI, HTTPException, Request

    # Request must be resolvable by FastAPI despite postponed annotations.
    globals()["Request"] = Request
    active = runtime or DesktopRuntime(config)
    expected_token = os.environ.get(config.desktop.webhook_token_env, "")

    @asynccontextmanager
    async def lifespan(app):
        try:
            await active.start()
            yield
        finally:
            await active.close()

    app = FastAPI(title="OpenVarak desktop runtime", lifespan=lifespan)
    app.state.runtime = active

    def authorize(request):
        if request.headers.get("origin"):
            raise HTTPException(403, "Browser-origin webhook calls are disabled")
        if expected_token and not secrets.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + expected_token
        ):
            raise HTTPException(401, "Invalid webhook token")

    @app.get("/health")
    async def health():
        return {
            "runtime": "openvarak",
            "status": "ready",
            "cluster": config.cluster.enabled,
            "orchestrator": config.desktop.orchestrator,
            "action_backend": config.desktop.action_backend,
            "inference": "not_checked",
        }

    @app.post("/webhooks/trigger", status_code=202)
    async def trigger(request: Request):
        authorize(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "Webhook body exceeds 1 MiB")
        try:
            payload = json.loads(body)
            job_id = active.submit(payload)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except asyncio.QueueFull as exc:
            raise HTTPException(429, "Desktop task queue is full") from exc
        return {"id": job_id, "status": active.job(job_id)["status"]}

    @app.get("/webhooks/events/{job_id}")
    async def job(job_id: str, request: Request):
        authorize(request)
        result = active.job(job_id)
        if result is None:
            raise HTTPException(404, "Unknown job")
        return result

    @app.get("/webhooks/events/{job_id}/actions")
    async def actions(job_id: str, request: Request):
        authorize(request)
        return [
            {k: row[k] for k in ("id", "tool", "state", "cost_minor", "currency")}
            for row in active.actions.actions(job_id)
        ]

    @app.post("/actions/{action_id}/reconcile")
    async def reconcile(action_id: str, request: Request):
        authorize(request)
        try:
            result = await asyncio.to_thread(active.reconcile_action, action_id)
            return {"success": result.success}
        except (ActionBlocked, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/webhooks/events/{job_id}/resume", status_code=202)
    async def resume(job_id: str, request: Request):
        authorize(request)
        try:
            active.resume(job_id)
        except (ActionBlocked, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        except asyncio.QueueFull as exc:
            raise HTTPException(429, "Desktop task queue is full") from exc
        return {"id": job_id, "status": "pending"}

    return app
