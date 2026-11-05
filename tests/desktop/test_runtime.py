from __future__ import annotations

import asyncio
import json
from importlib.metadata import entry_points
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from openvarak.agents._stubs import AgentResult
from openvarak.core.config import ClusterConfig, JarvisConfig, load_config
from openvarak.core.registry import ToolRegistry
from openvarak.core.types import Message, Role, ToolCall, ToolResult
from openvarak.desktop_cli import main
from openvarak.engine.desktop import SemanticComplexityRouter, resolve_endpoint
from openvarak.skills.synthesis import SkillSynthesizer, SynthesizeSkillTool
from openvarak.tools._stubs import ToolExecutor
from openvarak.tools.storage.persistent import HybridPersistentMemoryBackend


class FakeEngine:
    def __init__(self, text="done"):
        self.calls = []
        self.text = text

    def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return {"content": self.text, "usage": {}}

    async def stream(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        yield self.text

    async def stream_full(self, messages, **kwargs):
        from openvarak.engine._stubs import StreamChunk

        self.calls.append((messages, kwargs))
        yield StreamChunk(content=self.text, tool_calls=[{"name": "test"}])

    def health(self):
        return True

    def close(self):
        pass


class FakeSandbox:
    """Deterministic validation seam; never executes generated source on host."""

    def __init__(self):
        self.calls = []

    def execute_source(self, source, params):
        self.calls.append((source, params))
        if "broken" in source:
            raise RuntimeError("sandbox rejected")
        return {"ok": True}


def test_package_cli_and_default_profile(tmp_path, monkeypatch):
    import openvarak

    assert openvarak.__version__
    assert any(
        e.value == "openvarak.desktop_cli:main"
        for e in entry_points(group="console_scripts", name="varak")
    )
    assert CliRunner().invoke(main, ["--help"]).exit_code == 0
    monkeypatch.setenv("OPENVARAK_HOME", str(tmp_path))
    assert CliRunner().invoke(main, ["init"]).exit_code == 0
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.engine.default == "ollama"
    assert cfg.memory.default_backend == "hybrid_persistent"
    assert not cfg.telemetry.enabled and not cfg.cluster.enabled
    result = CliRunner().invoke(main, ["check"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["endpoint"] == "http://127.0.0.1:11434"


@pytest.mark.parametrize(
    "hint,expected",
    [
        ({}, "local"),
        ({"retry_count": 1}, "local"),
        ({"retry_count": 2}, "cloud"),
        ({"high_depth_reasoning": True}, "cloud"),
        ({"task_complexity": "architectural_synthesis"}, "cloud"),
        ({"task_complexity": "dom_json_parsing"}, "local"),
        ({"context_tokens": 10000}, "cloud"),
    ],
)
def test_routing(hint, expected):
    local, cloud = FakeEngine(), FakeEngine()
    router = SemanticComplexityRouter(
        local,
        cloud=cloud,
        local_model="local-model",
        cloud_model="provider/model",
        local_context_tokens=8192,
    )
    messages = [
        Message(role=Role.USER, content="tool/state query", metadata={"state": 7})
    ]
    result = router.generate(
        messages, model="ignored", tools=[{"name": "tool"}], **hint
    )
    selected = local if expected == "local" else cloud
    assert result["routing"]["tier"] == expected
    assert selected.calls[0][0] is messages
    assert selected.calls[0][1]["tools"] == [{"name": "tool"}]
    assert "retry_count" not in selected.calls[0][1]
    assert "high_depth_reasoning" not in selected.calls[0][1]


def test_default_local_never_builds_cloud():
    factory = MagicMock(side_effect=AssertionError("cloud must stay lazy"))
    router = SemanticComplexityRouter(
        FakeEngine(), local_model="local", cloud_factory=factory
    )
    for _ in range(100):
        router.generate([Message(role=Role.USER, content="routine")], model="local")
    factory.assert_not_called()


def test_schema_and_completion_count_toward_budget():
    router = SemanticComplexityRouter(
        FakeEngine(), local_model="local", cloud=FakeEngine(), local_context_tokens=2048
    )
    result = router.generate(
        [Message(role=Role.USER, content="short")],
        model="local",
        max_tokens=1000,
        tools=[{"description": "x" * 8000}],
    )
    assert result["routing"]["reason"] == "context"


def test_disabled_cloud_context_fails_without_losing_input():
    router = SemanticComplexityRouter(
        FakeEngine(), local_model="local", cloud_enabled=False, local_context_tokens=100
    )
    with pytest.raises(RuntimeError, match="budget exceeded"):
        router.generate([Message(role=Role.USER, content="hello")], model="local")


@pytest.mark.asyncio
async def test_stream_escalation_preserves_tool_chunks():
    cloud = FakeEngine()
    router = SemanticComplexityRouter(FakeEngine(), local_model="local", cloud=cloud)
    messages = [Message(role=Role.USER, content="hello")]
    assert [v async for v in router.stream(messages, model="local", retry_count=2)] == [
        "done"
    ]
    chunks = [
        v async for v in router.stream_full(messages, model="local", retry_count=2)
    ]
    assert chunks[0].tool_calls == [{"name": "test"}]
    assert cloud.calls[0][0] is messages


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://192.168.1.5:8000",
        "http://example.com:80",
        "file:///tmp/model",
        "http://user:pass@127.0.0.1:8000",
    ],
)
def test_cluster_rejects_remote_by_default(endpoint):
    with pytest.raises(ValueError):
        resolve_endpoint(endpoint, ClusterConfig())


def test_cluster_allowlist():
    endpoint = "http://192.168.1.5:8000"
    assert (
        resolve_endpoint("http://localhost:11434", ClusterConfig())
        == "http://127.0.0.1:11434"
    )
    assert resolve_endpoint(endpoint, ClusterConfig(True, [endpoint])) == endpoint
    with pytest.raises(ValueError):
        resolve_endpoint(endpoint, ClusterConfig(True, []))


def test_persistence_fts_replace_delete_and_checkpoint(tmp_path):
    path = tmp_path / "memory.db"
    memory = HybridPersistentMemoryBackend(path)
    doc_id = memory.store("oak timber construction", source="shed")
    memory.checkpoint("agent", {"step": 7})
    memory.close()
    memory = HybridPersistentMemoryBackend(path)
    assert memory.retrieve("oak")[0].source == "shed"
    assert memory.latest_checkpoint("agent") == {"step": 7}
    assert memory.retrieve('" OR *') == []
    memory.replace_source("shed", [("pine wood", {})])
    assert memory.retrieve("oak") == []
    assert not memory.delete(doc_id)
    assert memory.retrieve("pine")
    assert memory.pending_writes() == 4
    memory.clear()
    assert memory.count() == 0
    memory.close()


def test_outbox_failure_recovery_and_idempotent_deletes(tmp_path):
    fail = [True]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503 if fail[0] else 201, json={})

    path = tmp_path / "memory.db"
    kwargs = dict(
        supabase_url="https://test.supabase.co",
        supabase_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    memory = HybridPersistentMemoryBackend(path, **kwargs)
    doc_id = memory.store("retained while offline")
    with pytest.raises(httpx.HTTPStatusError):
        memory.sync_once()
    assert memory.retrieve("retained")
    memory.close()
    fail[0] = False
    memory = HybridPersistentMemoryBackend(path, **kwargs)
    assert memory.sync_once() == 1
    memory.delete(doc_id)
    assert memory.sync_once() == 1
    assert requests[-1].method == "DELETE"
    assert memory.pending_writes() == 0
    memory.close()


def test_missing_supabase_credentials_remains_local(tmp_path):
    memory = HybridPersistentMemoryBackend(tmp_path / "memory.db", sync_enabled=True)
    memory.store("offline")
    assert memory._worker is None
    assert memory.sync_once() == 0
    assert memory.retrieve("offline")
    memory.close()


def test_skill_synthesis_hot_registration(tmp_path):
    sandbox = FakeSandbox()
    synth = SkillSynthesizer(tmp_path, sandbox)
    executor = ToolExecutor([SynthesizeSkillTool(synth)])
    call = ToolCall(
        id="1",
        name="synthesize_skill",
        arguments=json.dumps(
            {
                "name": "new-skill",
                "description": "A tested skill",
                "source": "def execute(): return {}",
                "test_params": {},
                "expected_result": {"ok": True},
            }
        ),
    )
    result = executor.execute(call)
    assert result.success, result.content
    assert ToolRegistry.contains("new-skill")
    assert executor.execute(ToolCall(id="2", name="new-skill", arguments="{}")).success
    assert any(
        t["function"]["name"] == "new-skill" for t in executor.get_openai_tools()
    )
    assert (tmp_path / "new-skill" / "SKILL.md").read_text().startswith("---\nname:")
    assert len(sandbox.calls) == 2


def test_rejected_skill_never_registered_or_persisted(tmp_path):
    synth = SkillSynthesizer(tmp_path, FakeSandbox())
    with pytest.raises(RuntimeError, match="rejected"):
        synth.synthesize("bad-skill", "Bad", "def execute(): return 'broken'")
    assert not ToolRegistry.contains("bad-skill")
    assert not (tmp_path / "bad-skill").exists()
    with pytest.raises(ValueError):
        synth.synthesize("../escape", "Bad", "def execute(): return {}")


def test_self_heal_after_three_failures_resets_on_success(tmp_path):
    synth = SkillSynthesizer(tmp_path, FakeSandbox())
    tool = synth.synthesize("repair-me", "Test", "def execute(): return {}")
    tool.execute = lambda **kw: ToolResult(
        tool_name="repair-me", content="bad", success=False
    )
    executor = ToolExecutor([tool])
    patch = synth.synthesize(
        "repair-me", "Patched", "def execute(): return {}", replace=True
    )
    hook = MagicMock(return_value=patch)
    executor.configure_repair(hook, 3)
    call = ToolCall(id="1", name="repair-me", arguments="{}")
    executor.execute(call)
    executor.execute(call)
    hook.assert_not_called()
    result = executor.execute(call)
    assert result.metadata["skill_repaired"] is True
    assert executor.execute(call).success
    assert executor.retry_count == 0


@pytest.mark.asyncio
async def test_webhook_wakes_agent_without_polling_and_saves_state(tmp_path):
    from openvarak.runtime.daemon import DesktopRuntime

    cfg = JarvisConfig()
    memory = HybridPersistentMemoryBackend(tmp_path / "memory.db")
    agent = MagicMock()
    agent.run.return_value = AgentResult(content="completed", turns=2)
    runtime = DesktopRuntime(cfg, memory=memory, agent=agent, engine=FakeEngine())
    try:
        await runtime.start()
        job_id = runtime.submit({"prompt": "go now"})
        await asyncio.wait_for(runtime._queue.join(), 3)
        agent.run.assert_called_once_with("go now")
        assert runtime.job(job_id)["status"] == "completed"
        assert memory.latest_checkpoint("desktop")["content"] == "completed"
    finally:
        await runtime.close()


def test_webhook_auth_validation_and_backpressure(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from openvarak.runtime.daemon import DesktopRuntime, create_app

    cfg = JarvisConfig()
    memory = HybridPersistentMemoryBackend(tmp_path / "memory.db")
    agent = MagicMock()
    agent.run.return_value = AgentResult(content="done")
    runtime = DesktopRuntime(cfg, memory=memory, agent=agent, engine=FakeEngine())
    monkeypatch.setenv("VARAK_WEBHOOK_TOKEN", "secret")
    with TestClient(create_app(cfg, runtime=runtime)) as client:
        assert (
            client.post("/webhooks/trigger", json={"prompt": "test"}).status_code == 401
        )
        headers = {"Authorization": "Bearer secret"}
        assert (
            client.post("/webhooks/trigger", json=[], headers=headers).status_code
            == 422
        )
        response = client.post(
            "/webhooks/trigger", json={"prompt": "test"}, headers=headers
        )
        assert response.status_code == 202
        assert (
            client.get(
                "/webhooks/events/" + response.json()["id"], headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/webhooks/trigger",
                json={"prompt": "test"},
                headers={**headers, "Origin": "https://evil.test"},
            ).status_code
            == 403
        )


def test_router_default_does_not_initialize_sensors(tmp_path, monkeypatch):
    import openvarak.telemetry.energy_monitor as energy
    from openvarak.desktop_cli import desktop_config
    from openvarak.engine.desktop import build_desktop_engine

    monkeypatch.setattr(
        energy, "create_energy_monitor", MagicMock(side_effect=AssertionError())
    )
    engine = build_desktop_engine(desktop_config())
    assert engine._cloud is None
    engine.close()
    energy.create_energy_monitor.assert_not_called()


def test_optimizer_validates_patch_before_hot_reload(tmp_path):
    from openvarak.learning.agents.live_skill_optimizer import LiveSkillOptimizer

    synth = SkillSynthesizer(tmp_path, FakeSandbox())
    tool = synth.synthesize(
        "fix-tool", "Test", "def execute(): return {}", expected_result={"ok": True}
    )
    engine = FakeEngine(json.dumps({"source": "def execute(): return {'fixed': True}"}))
    optimizer = LiveSkillOptimizer(engine, "local", synth)
    call = ToolCall(id="1", name="fix-tool", arguments="{}")
    patched = optimizer(
        tool, call, ToolResult(tool_name="fix-tool", content="failed", success=False)
    )
    assert patched is not tool
    assert "fixed" in patched.source
    assert engine.calls[0][1]["retry_count"] == 2
    # A rejected patch leaves the last working registry entry and file intact.
    engine.text = json.dumps({"source": "def execute(): return 'broken'"})
    with pytest.raises(RuntimeError):
        optimizer(
            patched,
            call,
            ToolResult(tool_name="fix-tool", content="failed", success=False),
        )
    assert ToolRegistry.create("fix-tool") is patched
    assert "fixed" in (tmp_path / "fix-tool/scripts/tool.py").read_text()


@pytest.mark.asyncio
async def test_task_failure_triggers_bounded_optimizer_and_retries(tmp_path):
    from openvarak.engine.desktop import routing_hints
    from openvarak.runtime.daemon import DesktopRuntime

    memory = HybridPersistentMemoryBackend(tmp_path / "memory.db")
    hints = []

    def run(prompt):
        hints.append(dict(routing_hints.get()))
        if len(hints) <= 3:
            raise RuntimeError("failing step")
        return AgentResult(content="healed")

    agent = MagicMock()
    agent.run.side_effect = run
    runtime = DesktopRuntime(
        JarvisConfig(), memory=memory, agent=agent, engine=FakeEngine()
    )
    optimizer = MagicMock()
    optimizer.recover_task.return_value = "original task with recovery plan"
    runtime._optimizer = optimizer
    try:
        await runtime.start()
        job_id = runtime.submit({"prompt": "original task"})
        await asyncio.wait_for(runtime._queue.join(), 3)
        assert runtime.job(job_id)["status"] == "completed"
        optimizer.recover_task.assert_called_once()
        assert [h["retry_count"] for h in hints] == [0, 1, 2, 3]
        assert agent.run.call_args.args[0] == "original task with recovery plan"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_pending_job_recovers_after_restart(tmp_path):
    from openvarak.runtime.daemon import DesktopRuntime

    cfg = JarvisConfig()
    path = tmp_path / "memory.db"
    first = DesktopRuntime(
        cfg,
        memory=HybridPersistentMemoryBackend(path),
        agent=MagicMock(),
        engine=FakeEngine(),
    )
    job_id = first.submit({"prompt": "survives restart"})
    await first.close()
    agent = MagicMock()
    agent.run.return_value = AgentResult(content="recovered")
    second = DesktopRuntime(
        cfg,
        memory=HybridPersistentMemoryBackend(path),
        agent=agent,
        engine=FakeEngine(),
    )
    try:
        await second.start()
        await asyncio.wait_for(second._queue.join(), 3)
        assert second.job(job_id)["status"] == "completed"
        agent.run.assert_called_once_with("survives restart")
    finally:
        await second.close()


def test_webhook_queue_is_bounded_and_rejections_not_saved(tmp_path):
    from openvarak.runtime.daemon import DesktopRuntime

    cfg = JarvisConfig()
    cfg.desktop.queue_size = 1
    runtime = DesktopRuntime(
        cfg,
        memory=HybridPersistentMemoryBackend(tmp_path / "memory.db"),
        agent=MagicMock(),
        engine=FakeEngine(),
    )
    runtime.submit({"prompt": "accepted"})
    with pytest.raises(asyncio.QueueFull):
        runtime.submit({"prompt": "rejected"})
    assert runtime._db.execute("select count(*) from varak_jobs").fetchone()[0] == 1
    asyncio.run(runtime.close())


def test_browser_context_and_tabs_stay_on_one_owner_thread(tmp_path):
    import threading

    from openvarak.tools.playwright_session import PlaywrightSessionTool

    browser = PlaywrightSessionTool(tmp_path / "profile")
    calls = []

    def operate(**params):
        if browser._context is None:
            browser._context = MagicMock()
        calls.append((threading.get_ident(), browser._context, params))
        return {"state": "kept"}

    browser._operate = operate
    try:
        assert browser.execute(action="navigate", url="https://example.test").success
        assert browser.execute(action="click", selector="#button").success
        assert calls[0][0] == calls[1][0] != threading.get_ident()
        assert calls[0][1] is calls[1][1]
    finally:
        browser.close()


def test_skill_sandbox_uses_isolated_container(monkeypatch):
    from openvarak.sandbox.skills import SkillSandbox

    sandbox = SkillSandbox()
    monkeypatch.setattr(sandbox, "_check_runtime", lambda: "/usr/bin/docker")
    captured = []

    def run(args, **kwargs):
        captured.append((args, kwargs))
        kwargs["stdout"].write('VARAK_RESULT={"ok": true}\n')
        return MagicMock(returncode=0)

    monkeypatch.setattr("subprocess.run", run)
    assert sandbox.execute_source("def execute(): return {}", {}) == {"ok": True}
    args, kwargs = captured[0]
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args and "--cap-drop=ALL" in args
    assert "--memory" in args and "--pids-limit" in args
    assert kwargs["timeout"] == 30


def test_openrouter_http_contract_preserves_tool_call_state(monkeypatch):
    from openvarak.engine.desktop import OpenRouterEngine

    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    # Avoid environment proxy dependencies; transport below never leaves process.
    monkeypatch.setenv("NO_PROXY", "*")
    engine = OpenRouterEngine(trust_env=False)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "next",
                                    "type": "function",
                                    "function": {"name": "test", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    engine._client.close()
    engine._client = httpx.Client(
        base_url=engine._host,
        transport=httpx.MockTransport(handler),
        headers=engine._headers,
    )
    try:
        result = engine.generate(
            [Message(role=Role.TOOL, content="state", tool_call_id="prior")],
            model="openrouter/auto",
            tools=[{"type": "function"}],
        )
        assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(seen[0].content)
        assert body["messages"][0]["tool_call_id"] == "prior"
        assert body["model"] == "openrouter/auto"
        assert result["tool_calls"][0]["name"] == "test"
    finally:
        engine.close()


def test_saved_skills_revalidate_and_load_agent_skills_manifest(tmp_path):
    from openvarak.skills.loader import load_skill_markdown

    sandbox = FakeSandbox()
    first = SkillSynthesizer(tmp_path, sandbox)
    first.synthesize("saved-skill", "Persisted tool", "def execute(): return {}")
    manifest = load_skill_markdown(tmp_path / "saved-skill" / "SKILL.md")
    assert manifest.name == "saved-skill"
    second = SkillSynthesizer(tmp_path, sandbox)
    assert second.load_saved()[0].spec.name == "saved-skill"
    assert len(sandbox.calls) == 2
