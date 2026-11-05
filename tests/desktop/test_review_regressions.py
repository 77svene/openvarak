"""Regressions found while reviewing the merged desktop runtime."""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from openvarak.agents._stubs import AgentResult
from openvarak.core.config import JarvisConfig
from openvarak.core.registry import MemoryRegistry
from openvarak.core.types import ToolCall
from openvarak.runtime.daemon import DesktopRuntime
from openvarak.sdk import MemoryHandle
from openvarak.system.builder import SystemBuilder
from openvarak.tools.storage import create_memory_backend
from openvarak.tools.storage.persistent import HybridPersistentMemoryBackend


@pytest.mark.parametrize(
    "entrypoint", ["factory", "ask", "memory", "sdk", "builder", "server"]
)
def test_memory_entrypoints_honor_config_after_registry_reset(tmp_path, entrypoint):
    config = JarvisConfig()
    config.memory.db_path = str(tmp_path / "configured.db")
    MemoryRegistry.clear()
    ask = importlib.import_module("openvarak.cli.ask")
    memory_cmd = importlib.import_module("openvarak.cli.memory_cmd")
    server = importlib.import_module("openvarak.server.agent_manager_routes")
    from unittest.mock import patch

    with patch.object(memory_cmd, "load_config", return_value=config):
        factories = {
            "factory": lambda: create_memory_backend(config),
            "ask": lambda: ask._get_memory_backend(config),
            "memory": memory_cmd._get_backend,
            "sdk": lambda: MemoryHandle(config)._get_backend(),
            "builder": lambda: SystemBuilder(config)._resolve_memory(config),
            "server": lambda: server._resolve_memory_backend(config),
        }
        backend = factories[entrypoint]()
    try:
        assert backend is not None
        assert backend._db_path == config.memory.db_path
        backend.store("stored in the selected database")
    finally:
        backend.close()
    reopened = HybridPersistentMemoryBackend(config.memory.db_path)
    try:
        assert reopened.retrieve("selected")
    finally:
        reopened.close()


def test_memory_factory_forwards_optional_sync_configuration(tmp_path, monkeypatch):
    config = JarvisConfig()
    config.memory.db_path = str(tmp_path / "configured.db")
    config.memory.supabase_enabled = True
    factory = MagicMock(return_value=object())
    monkeypatch.setattr(
        "openvarak.tools.storage.persistent.HybridPersistentMemoryBackend.from_config",
        factory,
    )
    MemoryRegistry.clear()
    assert create_memory_backend(config) is factory.return_value
    factory.assert_called_once_with(config)


def make_runtime(tmp_path, *, config=None, agent=None, engine=None):
    config = config or JarvisConfig()
    agent = agent or MagicMock()
    agent.run.return_value = AgentResult(content="done")
    engine = engine or MagicMock()
    return DesktopRuntime(
        config,
        agent=agent,
        engine=engine,
        memory=HybridPersistentMemoryBackend(tmp_path / "memory.db"),
    )


@pytest.mark.asyncio
async def test_restart_with_reduced_queue_does_not_wait_for_old_tasks(tmp_path):
    config = JarvisConfig()
    config.desktop.queue_size = 3
    first = make_runtime(tmp_path, config=config)
    jobs = [first.submit({"prompt": f"job {i}"}) for i in range(3)]
    await first.close()

    config.desktop.queue_size = 1
    release = threading.Event()
    agent = MagicMock()
    agent.run.side_effect = lambda prompt: (
        release.wait(5) and AgentResult(content="done")
    )
    second = make_runtime(tmp_path, config=config, agent=agent)
    try:
        await asyncio.wait_for(second.start(), 1)
        with pytest.raises(asyncio.QueueFull):
            second.submit({"prompt": "queue still full on disk"})
        release.set()
        await asyncio.wait_for(second._queue.join(), 3)
        assert all(second.job(job)["status"] == "completed" for job in jobs)
        assert agent.run.call_count == 3
    finally:
        release.set()
        await second.close()


@pytest.mark.asyncio
async def test_corrupt_persisted_job_does_not_kill_worker(tmp_path):
    runtime = make_runtime(tmp_path)
    broken = runtime.submit({"prompt": "corrupt me"})
    good = runtime.submit({"prompt": "valid"})
    with runtime._db:
        runtime._db.execute("UPDATE varak_jobs SET payload='{' WHERE id=?", (broken,))
    try:
        await runtime.start()
        await asyncio.wait_for(runtime._queue.join(), 3)
        assert runtime.job(broken)["status"] == "failed"
        assert runtime.job(good)["status"] == "completed"
        assert not runtime._worker.done()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_cleanup_failure_still_closes_databases_and_engine(tmp_path):
    engine = MagicMock()
    runtime = make_runtime(tmp_path, engine=engine)
    broken_resource = MagicMock()
    broken_resource.close.side_effect = RuntimeError("browser cleanup failed")
    runtime._owned_tools.append(broken_resource)
    await runtime.close()
    await runtime.close()
    engine.close.assert_called_once()
    with pytest.raises(sqlite3.ProgrammingError):
        runtime._db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        runtime.memory.count()


def test_zero_queue_capacity_is_rejected_before_creating_resources():
    config = JarvisConfig()
    config.desktop.queue_size = 0
    with pytest.raises(ValueError, match="queue_size"):
        DesktopRuntime(config)


@pytest.mark.asyncio
async def test_daemon_wires_policy_before_synthesized_code_execution(
    tmp_path, monkeypatch
):
    class DenyAll:
        def check(self, *args):
            return False

    policy = DenyAll()
    config = JarvisConfig()
    config.security.capabilities.enabled = True
    engine = MagicMock()
    engine.engine_id = "desktop"
    setup = MagicMock(
        return_value=SimpleNamespace(
            engine=engine,
            capability_policy=policy,
            rate_limiter=None,
            audit_logger=None,
        )
    )
    monkeypatch.setattr("openvarak.security.setup_security", setup)
    runtime = DesktopRuntime(
        config,
        engine=engine,
        memory=HybridPersistentMemoryBackend(tmp_path / "memory.db"),
    )
    try:
        agent = runtime._build_agent()
        assert agent._executor._capability_policy is policy
        synth = agent._executor._tools["synthesize_skill"]
        sandbox = MagicMock(side_effect=AssertionError("denied code must not execute"))
        monkeypatch.setattr(synth.synthesizer.sandbox, "execute_source", sandbox)
        result = agent._executor.execute(
            ToolCall(id="deny", name="synthesize_skill", arguments="{}")
        )
        assert not result.success
        sandbox.assert_not_called()
        setup.assert_called_once()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_repeated_start_keeps_one_worker_and_closed_runtime_cannot_restart(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    await runtime.start()
    first_worker = runtime._worker
    await runtime.start()
    assert runtime._worker is first_worker
    await runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        await runtime.start()


def test_connector_registry_reset_preserves_injected_adapter(monkeypatch):
    from openvarak.connectors import granola
    from openvarak.core.registry import ConnectorRegistry
    from openvarak.server.connectors_router import _ensure_connectors_registered

    original_class = granola.GranolaConnector
    validator = MagicMock()
    monkeypatch.setattr(granola, "_granola_api_validate_key", validator)
    ConnectorRegistry.clear()
    _ensure_connectors_registered()
    assert ConnectorRegistry.get("granola") is original_class
    assert granola._granola_api_validate_key is validator


@pytest.mark.asyncio
async def test_automatic_skill_repair_cannot_bypass_denied_file_write(
    tmp_path, monkeypatch
):
    from openvarak.skills.synthesis import SynthesizedSkillTool

    class NoFileWrite:
        def check(self, agent_id, capability, resource):
            return capability != "file:write"

    sandbox = MagicMock()
    sandbox.execute_source.side_effect = RuntimeError("tool failed")
    tool = SynthesizedSkillTool(
        "existing-skill", "Existing", {}, "def execute(): pass", sandbox
    )
    monkeypatch.setattr(
        "openvarak.skills.synthesis.SkillSynthesizer.load_saved", lambda self: [tool]
    )
    engine = MagicMock()
    engine.engine_id = "desktop"
    monkeypatch.setattr(
        "openvarak.security.setup_security",
        lambda *args: SimpleNamespace(
            engine=engine,
            capability_policy=NoFileWrite(),
            rate_limiter=None,
            audit_logger=None,
        ),
    )
    runtime = DesktopRuntime(
        JarvisConfig(),
        engine=engine,
        memory=HybridPersistentMemoryBackend(tmp_path / "memory.db"),
    )
    try:
        agent = runtime._build_agent()
        optimizer = MagicMock()
        runtime._optimizer = optimizer
        call = ToolCall(id="failed", name="existing-skill", arguments="{}")
        for _ in range(3):
            result = agent._executor.execute(call)
        assert not result.success
        assert result.metadata["repair_denied"] is True
        optimizer.assert_not_called()
    finally:
        await runtime.close()
