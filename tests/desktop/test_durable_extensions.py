"""Execution contracts plus real optional-service integration tests."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from openvarak.agents._stubs import AgentResult
from openvarak.core.config import JarvisConfig
from openvarak.core.types import ToolCall, ToolResult
from openvarak.runtime.actions import (
    ActionBlocked,
    ActionJournal,
    action_deadline,
    action_job,
)
from openvarak.runtime.daemon import DesktopRuntime
from openvarak.runtime.verification import verify
from openvarak.tools._stubs import BaseTool, ToolExecutor, ToolSpec
from openvarak.tools.storage.persistent import HybridPersistentMemoryBackend


class Engine:
    def close(self):
        pass


def make_runtime(path, agent, config=None):
    return DesktopRuntime(
        config or JarvisConfig(),
        agent=agent,
        engine=Engine(),
        memory=HybridPersistentMemoryBackend(path / "memory.db"),
    )


class FixtureTool(BaseTool):
    tool_id = "write_fixture"

    def __init__(self, *, crash=False, delay=0):
        self.crash, self.delay = crash, delay
        self.calls = 0

    @property
    def spec(self):
        return ToolSpec(
            name=self.tool_id,
            description="Append one fixture effect",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            timeout_seconds=0.02 if self.delay else 30,
        )

    def execute(self, **params):
        self.calls += 1
        with sqlite3.connect(params["path"]) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS effects (id INTEGER PRIMARY KEY, note TEXT)"
            )
            db.execute("INSERT INTO effects(note) VALUES ('done')")
        if self.crash:
            os._exit(73)
        if self.delay:
            time.sleep(self.delay)
        return ToolResult(tool_name=self.tool_id, content="written", success=True)

    def reconcile_action(self, **params):
        with sqlite3.connect(params["path"]) as db:
            count = db.execute("SELECT count(*) FROM effects").fetchone()[0]
        return ToolResult(tool_name=self.tool_id, content="written", success=count == 1)


class FixtureAgent:
    def __init__(self, path, *, crash=False, fail=False):
        self._executor = ToolExecutor([FixtureTool(crash=crash)])
        self.path, self.fail, self.calls = path, fail, 0

    def run(self, prompt):
        self.calls += 1
        result = self._executor.execute(
            ToolCall(
                id=uuid.uuid4().hex,
                name="write_fixture",
                arguments=json.dumps({"path": str(self.path)}),
            )
        )
        return AgentResult(
            content="done",
            tool_results=[result],
            metadata={"error": True} if self.fail else {},
        )


@pytest.fixture(scope="session")
def pg_uri(tmp_path_factory):
    uri = os.environ.get("VARAK_TEST_POSTGRES_DSN")
    if uri:
        yield uri
        return
    pytest.importorskip(
        "pgserver", reason="Install pgserver or supply VARAK_TEST_POSTGRES_DSN"
    )
    import socket

    from pgserver._commands import initdb, pg_ctl

    directory = tmp_path_factory.mktemp("native-postgres")
    data = directory / "data"
    initdb(
        ["--auth=trust", "--encoding=utf8", "-U", "postgres"], pgdata=data, timeout=30
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    pg_ctl(
        [
            "-w",
            "-l",
            str(directory / "postgres.log"),
            "-o",
            f"-h 127.0.0.1 -p {port} -c unix_socket_directories=''",
            "start",
        ],
        pgdata=data,
        timeout=30,
    )
    try:
        yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    finally:
        pg_ctl(["-w", "-m", "fast", "stop"], pgdata=data, timeout=30)


@pytest.fixture(params=["sqlite", "postgres"])
def journal(request, tmp_path):
    if request.param == "sqlite":
        j = ActionJournal(tmp_path / "actions.db", budget_minor=1000)
        yield j
        j.close()
    else:
        psycopg = pytest.importorskip("psycopg")
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        uri = request.getfixturevalue("pg_uri")
        schema = "test_" + uuid.uuid4().hex
        with psycopg.connect(uri, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        dsn = make_conninfo(uri, options="-c search_path=" + schema)
        j = ActionJournal(dsn, backend="postgres", budget_minor=1000)
        try:
            yield j
        finally:
            j.close()
            with psycopg.connect(uri, autocommit=True) as admin:
                admin.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )


def success(name="write_fixture"):
    return ToolResult(tool_name=name, content="ok", success=True)


def test_journal_receipt_replay(journal):
    key, cached = journal.claim("job", "write_fixture", {"target": 1}, cost_minor=100)
    assert cached is None
    journal.finish(key, success())
    again, cached = journal.claim("job", "write_fixture", {"target": 1}, cost_minor=100)
    assert again == key and cached.success and cached.metadata["journal_replay"]


def test_journal_blocks_unknown_and_changed_cost(journal):
    key, _ = journal.claim("job", "write_fixture", {}, cost_minor=800)
    journal.unknown(key)
    with pytest.raises(ActionBlocked):
        journal.claim("job", "write_fixture", {}, cost_minor=800)
    with pytest.raises(ActionBlocked):
        journal.claim("job", "write_fixture", {"different": 1})
    with pytest.raises(ActionBlocked):
        journal.claim("different-job", "write_fixture", {}, cost_minor=300)
    with pytest.raises(ActionBlocked):
        journal.claim("job", "write_fixture", {}, cost_minor=801)


def test_authoritative_reconciliation(journal):
    key, _ = journal.claim("job", "write_fixture", {})
    journal.recover(["job"])
    with pytest.raises(ActionBlocked):
        journal.reconcile(key, lambda name, params: None)
    journal.reconcile(key, lambda name, params: success(name))
    assert journal.claim("job", "write_fixture", {})[1].success


def test_settlement_ledger_balances_and_deduplicates(journal):
    key, _ = journal.claim("job", "write_fixture", {}, cost_minor=200)
    journal.finish(key, success())
    for _ in range(2):
        journal.record_settlement(
            "receipt", key, revenue_minor=500, cost_minor=150, currency="CAD"
        )
    assert journal.totals()["contribution_minor"] == 350
    rows = journal._sql(
        "SELECT SUM(debit_minor-credit_minor) AS balance FROM varak_ledger"
    ).fetchone()
    assert rows["balance"] == 0
    with pytest.raises(ActionBlocked):
        journal.record_settlement(
            "receipt", key, revenue_minor=501, cost_minor=150, currency="CAD"
        )
    with pytest.raises(ActionBlocked):
        journal.record_settlement(
            "other", key, revenue_minor=1, cost_minor=201, currency="CAD"
        )


def test_budget_cannot_be_lowered_below_commitments(journal):
    journal.claim("job", "write_fixture", {}, cost_minor=200)
    with pytest.raises(ActionBlocked):
        journal.set_budget(199)
    journal.set_budget(200)


@pytest.mark.parametrize("bad", [-1, True, 0.5, "1"])
def test_invalid_cost_rejected(tmp_path, bad):
    j = ActionJournal(tmp_path / "actions.db")
    try:
        with pytest.raises(ValueError):
            j.claim("job", "fixture", {}, cost_minor=bad)
    finally:
        j.close()


def test_existing_confirmation_gate_precedes_journal(tmp_path):
    class ConfirmTool(FixtureTool):
        @property
        def spec(self):
            result = super().spec
            result.requires_confirmation = True
            return result

    j = ActionJournal(tmp_path / "actions.db")
    tool = ConfirmTool()
    executor = ToolExecutor([tool])
    executor.configure_journal(j)
    token = action_job.set("job")
    try:
        result = executor.execute(
            ToolCall(id="one", name="write_fixture", arguments="{}")
        )
        assert not result.success and tool.calls == 0
        assert j.actions("job") == []
    finally:
        action_job.reset(token)
        j.close()


def test_existing_capability_gate_precedes_journal(tmp_path):
    class Deny:
        def check(self, *args):
            return False

    class RestrictedTool(FixtureTool):
        @property
        def spec(self):
            result = super().spec
            result.required_capabilities = ["file:write"]
            return result

    j = ActionJournal(tmp_path / "actions.db")
    tool = RestrictedTool()
    executor = ToolExecutor([tool], capability_policy=Deny())
    executor.configure_journal(j)
    token = action_job.set("job")
    try:
        result = executor.execute(
            ToolCall(id="one", name="write_fixture", arguments="{}")
        )
        assert not result.success and tool.calls == 0
        assert j.actions("job") == []
    finally:
        action_job.reset(token)
        j.close()


def test_concurrent_reservations(journal):
    owner = journal

    def reserve(i):
        j = ActionJournal(journal.target, backend=journal.backend, budget_minor=1000)
        try:
            j.claim(str(i), "fixture", {}, cost_minor=100)
            return True
        except ActionBlocked:
            return False
        finally:
            j.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(reserve, range(50))) == 10
    assert owner.totals()["reserved_minor"] == 1000


def test_expired_action_and_wrong_receipt(journal):
    token = action_deadline.set(0)
    try:
        with pytest.raises(ActionBlocked, match="expired"):
            journal.claim("job", "fixture", {})
    finally:
        action_deadline.reset(token)
    key, _ = journal.claim("job", "fixture", {})
    with pytest.raises(ActionBlocked, match="different tool"):
        journal.finish(key, success("wrong"))


@pytest.mark.asyncio
async def test_duplicate_events_and_payload_collision(tmp_path):
    agent = FixtureAgent(tmp_path / "effects.db")
    rt = make_runtime(tmp_path, agent)
    try:
        p = {"prompt": "once", "event_id": "same"}
        first = rt.submit(p)
        assert rt.submit(dict(p)) == first
        with pytest.raises(ValueError, match="different payload"):
            rt.submit({**p, "prompt": "changed"})
        await rt.start()
        await asyncio.wait_for(rt._queue.join(), 5)
        assert agent.calls == 1
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_model_claim_is_not_verified(tmp_path):
    class Claim:
        def run(self, prompt):
            return AgentResult(content="I fixed it")

    rt = make_runtime(tmp_path, Claim())
    try:
        await rt.start()
        job = rt.submit({"prompt": "fix it"})
        await asyncio.wait_for(rt._queue.join(), 5)
        assert rt.job(job)["status"] == "unverified"
        assert rt.job(job)["result"]["verification"]["state"] == "unverified"
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_failed_task_does_not_repeat_prior_action(tmp_path):
    agent = FixtureAgent(tmp_path / "effects.db", fail=True)
    rt = make_runtime(tmp_path, agent)
    try:
        await rt.start()
        job = rt.submit({"prompt": "once"})
        await asyncio.wait_for(rt._queue.join(), 5)
        assert agent.calls == 1
        assert rt.job(job)["status"] == "needs_reconciliation"
        with sqlite3.connect(agent.path) as db:
            assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_tool_replay_ignores_new_model_call_id(tmp_path):
    tool = FixtureTool()
    executor = ToolExecutor([tool])

    class Twice:
        _executor = executor

        def run(self, prompt):
            results = [
                executor.execute(
                    ToolCall(
                        id=str(i),
                        name="write_fixture",
                        arguments=json.dumps({"path": str(tmp_path / "effects.db")}),
                    )
                )
                for i in range(2)
            ]
            return AgentResult(content="done", tool_results=results)

    rt = make_runtime(tmp_path, Twice())
    try:
        await rt.start()
        job = rt.submit(
            {
                "prompt": "once",
                "acceptance": {"kind": "tool_receipts", "tools": ["write_fixture"]},
            }
        )
        await asyncio.wait_for(rt._queue.join(), 5)
        assert tool.calls == 1
        assert rt.job(job)["status"] == "completed"
    finally:
        await rt.close()


def test_late_tool_receipt_resolves_timeout_without_replay(tmp_path):
    j = ActionJournal(tmp_path / "actions.db")
    tool = FixtureTool(delay=0.15)
    executor = ToolExecutor([tool])
    executor.configure_journal(j)
    token = action_job.set("job")
    try:
        call = ToolCall(
            id="one",
            name="write_fixture",
            arguments=json.dumps({"path": str(tmp_path / "effects.db")}),
        )
        assert not executor.execute(call).success
        assert not executor.execute(call).success
        deadline = time.monotonic() + 3
        while j.unresolved("job") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert executor.execute(call).success
        assert tool.calls == 1
    finally:
        action_job.reset(token)
        j.close()


@pytest.mark.asyncio
async def test_runtime_lease_blocks_second_worker(tmp_path):
    first = make_runtime(tmp_path, FixtureAgent(tmp_path / "effects.db"))
    second = make_runtime(tmp_path, FixtureAgent(tmp_path / "effects.db"))
    try:
        await first.start()
        with pytest.raises(RuntimeError, match="Another desktop"):
            await second.start()
    finally:
        await second.close()
        await first.close()


def test_artifact_contracts_and_traversal(tmp_path):
    data = b'{"accepted":true}'
    (tmp_path / "result.json").write_bytes(data)
    assert (
        verify(
            {
                "kind": "file_sha256",
                "path": "result.json",
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            {},
            tmp_path,
        )["state"]
        == "verified"
    )
    assert (
        verify(
            {
                "kind": "json_equals",
                "path": "result.json",
                "expected": {"accepted": False},
            },
            {},
            tmp_path,
        )["state"]
        == "failed"
    )
    with pytest.raises(ValueError):
        verify(
            {"kind": "file_sha256", "path": "../outside", "sha256": "0" * 64},
            {},
            tmp_path,
        )


def test_evidence_scope_freshness_and_restart(tmp_path):
    path = tmp_path / "memory.db"
    memory = HybridPersistentMemoryBackend(path)
    memory.store_evidence(
        "oak demand",
        source_uri="https://example.test/a",
        scope="a",
        observed_at=1,
        valid_until=20,
    )
    memory.store_evidence(
        "oak demand",
        source_uri="https://example.test/b",
        scope="b",
        observed_at=1,
        valid_until=20,
    )
    memory.store("oak demand", source="checkpoint")
    memory.close()
    memory = HybridPersistentMemoryBackend(path)
    try:
        result = memory.retrieve_evidence("oak", scope="a", now=10)
        assert len(result) == 1 and result[0].source.endswith("/a")
        assert memory.retrieve_evidence("oak", scope="a", now=21) == []
        assert memory.retrieve_evidence("oak", scope="unknown", now=10) == []
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_expired_job_never_runs_agent(tmp_path):
    agent = FixtureAgent(tmp_path / "effects.db")
    rt = make_runtime(tmp_path, agent)
    try:
        with pytest.raises(ValueError):
            rt.submit({"prompt": "bad", "acceptance": {"kind": "unknown"}})
        await rt.start()
        job = rt.submit({"prompt": "expired", "expires_at": 1})
        await asyncio.wait_for(rt._queue.join(), 3)
        assert rt.job(job)["status"] == "expired" and agent.calls == 0
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_real_temporal_runtime_selection(tmp_path):
    pytest.importorskip("temporalio")
    from temporalio.testing import WorkflowEnvironment

    async with await WorkflowEnvironment.start_time_skipping() as env:
        cfg = JarvisConfig()
        cfg.desktop.orchestrator = "temporal"
        cfg.desktop.temporal_address = env.client.service_client.config.target_host
        rt = make_runtime(tmp_path, FixtureAgent(tmp_path / "effects.db"), cfg)
        try:
            await rt.start()
            job = rt.submit({"prompt": "once", "acceptance": {"kind": "tool_receipts"}})
            await asyncio.wait_for(rt._queue.join(), 15)
            assert rt.job(job)["status"] == "completed"
            assert rt._agent.calls == 1
        finally:
            await rt.close()


@pytest.mark.asyncio
async def test_real_pydantic_loopback_http_runtime(tmp_path):
    pytest.importorskip("pydantic_ai")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            output = next(
                t["function"]["name"]
                for t in body["tools"]
                if t["function"]["name"].startswith("final_result")
            )
            result = {
                "id": "local-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "result",
                                    "type": "function",
                                    "function": {
                                        "name": output,
                                        "arguments": json.dumps(
                                            {
                                                "summary": "local typed response",
                                                "artifacts": [],
                                                "limitations": [],
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            }
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = JarvisConfig()
    cfg.desktop.agent_type = "pydantic"
    cfg.desktop.skills_enabled = False
    cfg.engine.default = "llamacpp"
    cfg.engine.llamacpp_host = f"http://127.0.0.1:{server.server_port}"
    cfg.intelligence.default_model = "local-main"
    cfg.tools.enabled = "calculator"
    rt = DesktopRuntime(
        cfg,
        engine=Engine(),
        memory=HybridPersistentMemoryBackend(tmp_path / "memory.db"),
    )
    try:
        await rt.start()
        job = rt.submit({"prompt": "hello"})
        await asyncio.wait_for(rt._queue.join(), 15)
        result = rt.job(job)
        assert result["status"] == "unverified", result
        assert result["result"]["typed_output"]["summary"] == "local typed response"
        assert len(requests) == 1 and requests[0]["model"] == "local-main"
    finally:
        await rt.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.asyncio
async def test_real_process_crash_reconciles_then_resumes_once(tmp_path):
    child = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--crash-worker",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 73, child.stderr
    job = (tmp_path / "job-id").read_text()
    agent = FixtureAgent(tmp_path / "effects.db")
    rt = make_runtime(tmp_path, agent)
    try:
        await rt.start()
        assert rt.job(job)["status"] == "needs_reconciliation"
        rows = rt.actions.actions(job)
        assert rows[0]["state"] == "unknown"
        rt.reconcile_action(rows[0]["id"])
        rt.resume(job)
        await asyncio.wait_for(rt._queue.join(), 5)
        assert rt.job(job)["status"] == "completed"
        assert agent._executor._tools["write_fixture"].calls == 0
        with sqlite3.connect(tmp_path / "effects.db") as db:
            assert db.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
    finally:
        await rt.close()


def test_real_pydantic_agent_uses_existing_executor(tmp_path):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from openvarak.agents.pydantic_desktop import PydanticDesktopAgent

    calls = []

    def model(messages, info):
        calls.append(messages)
        if len(calls) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "write_fixture", {"path": str(tmp_path / "effects.db")}
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"summary": "done", "artifacts": [], "limitations": []},
                )
            ]
        )

    tool = FixtureTool()
    agent = PydanticDesktopAgent(
        JarvisConfig(), tools=[tool], model=FunctionModel(model)
    )
    result = agent.run("write the fixture")
    assert result.content == "done" and tool.calls == 1
    assert result.metadata["typed_output"]["summary"] == "done"


@pytest.mark.asyncio
async def test_real_temporal_dispatch_and_duplicate_workflow(tmp_path):
    pytest.importorskip("temporalio")
    from temporalio.testing import WorkflowEnvironment

    from openvarak.runtime.temporal import TemporalDispatcher

    rt = make_runtime(tmp_path, FixtureAgent(tmp_path / "effects.db"))
    async with await WorkflowEnvironment.start_time_skipping() as env:
        dispatcher = TemporalDispatcher(rt, rt.config.desktop, client=env.client)
        try:
            await dispatcher.start()
            job = rt.submit(
                {
                    "prompt": "once",
                    "acceptance": {"kind": "tool_receipts", "tools": ["write_fixture"]},
                }
            )
            # Injected runtime does not start a second queue consumer here.
            rt._agent._executor.configure_journal(rt.actions)
            await dispatcher.dispatch(job)
            await dispatcher.dispatch(job)
            assert rt.job(job)["status"] == "completed"
            assert rt._agent.calls == 1
        finally:
            await dispatcher.close()
            await rt.close()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--crash-worker":
    directory = Path(sys.argv[2])

    async def crash_worker():
        rt = make_runtime(directory, FixtureAgent(directory / "effects.db", crash=True))
        await rt.start()
        job = rt.submit(
            {
                "prompt": "once",
                "acceptance": {"kind": "tool_receipts", "tools": ["write_fixture"]},
            }
        )
        (directory / "job-id").write_text(job)
        await rt._queue.join()

    asyncio.run(crash_worker())
