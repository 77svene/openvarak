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
from types import SimpleNamespace

from openvarak.engine.desktop import build_desktop_engine
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
        self._job_lock = threading.RLock()
        self._db = sqlite3.connect(self.memory._db_path, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("""CREATE TABLE IF NOT EXISTS varak_jobs (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, created_at REAL NOT NULL)""")
        self._db.commit()

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
        if self._agent is None:
            self._agent = await asyncio.to_thread(self._build_agent)
        # Recover unacknowledged jobs before accepting more work.
        with self._job_lock, self._db:
            self._db.execute(
                "UPDATE varak_jobs SET status='pending' WHERE status='running'"
            )
            rows = self._db.execute(
                "SELECT id FROM varak_jobs WHERE status='pending' ORDER BY created_at"
            ).fetchall()
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
        job_id = uuid.uuid4().hex
        if self._queue.full():
            raise asyncio.QueueFull
        with self._job_lock, self._db:
            self._db.execute(
                "INSERT INTO varak_jobs VALUES (?,?,'pending',NULL,?)",
                (job_id, json.dumps(payload), time.time()),
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
                with self._job_lock:
                    row = self._db.execute(
                        "SELECT payload FROM varak_jobs WHERE id=?", (job_id,)
                    ).fetchone()
                self._status(job_id, "running")
                payload = json.loads(row[0])
                try:
                    result = await asyncio.to_thread(self._run, payload, job_id)
                    self.memory.checkpoint("desktop", result)
                    self._status(job_id, "completed", result)
                except Exception as exc:
                    logger.warning(
                        "Desktop job %s failed: %s", job_id, type(exc).__name__
                    )
                    self._status(job_id, "failed", {"error": str(exc)})
            finally:
                self._queue.task_done()

    def _run(self, payload, job_id):
        from openvarak.engine.desktop import routing_hints

        failures = []
        threshold = max(1, self.config.desktop.self_heal_threshold)
        prompt = payload["prompt"]
        # N attempts followed by at most one optimizer-assisted attempt.
        for attempt in range(threshold + 1):
            token = routing_hints.set(
                {
                    "high_depth_reasoning": payload.get("high_depth_reasoning") is True,
                    "retry_count": attempt,
                }
            )
            try:
                if attempt == threshold:
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
                self.memory.checkpoint("desktop", trace, kind="trace")
                return trace
            except PermissionError:
                raise
            except Exception as exc:
                failures.append(str(exc))
                self.memory.checkpoint(
                    job_id, {"attempt": attempt, "error": str(exc)}, kind="failed_trace"
                )
                if attempt == threshold:
                    raise
            finally:
                routing_hints.reset(token)

    async def close(self):
        self._stopping = True
        if self._worker:
            if not self._worker.done() and self._queue.empty():
                self._queue.put_nowait(None)
            await self._worker  # finish active job; pending work stays durable
        for tool in reversed(self._owned_tools):
            await asyncio.to_thread(tool.close)
        self.engine.close()
        self._db.close()
        await asyncio.to_thread(self.memory.close)


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
        return {"id": job_id, "status": "pending"}

    @app.get("/webhooks/events/{job_id}")
    async def job(job_id: str, request: Request):
        authorize(request)
        result = active.job(job_id)
        if result is None:
            raise HTTPException(404, "Unknown job")
        return result

    return app
