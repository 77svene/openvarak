"""Single-desktop routing. No discovery, model enumeration, or sensor polling."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from openvarak.core.types import Message
from openvarak.engine._base import InferenceEngine, estimate_prompt_tokens
from openvarak.engine._openai_compat import _OpenAICompatibleEngine

routing_hints = ContextVar("varak_routing_hints", default={})


def resolve_endpoint(endpoint: str, cluster: Any) -> str:
    """Normalize loopback; remote endpoints must be explicitly allowlisted."""
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Inference endpoint must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Endpoint credentials, query, and fragment are unsupported")
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return urlunsplit(
            (
                parsed.scheme,
                f"127.0.0.1:{parsed.port}" if parsed.port else "127.0.0.1",
                parsed.path.rstrip("/"),
                "",
                "",
            )
        )
    if not cluster.enabled or endpoint.rstrip("/") not in cluster.remote_nodes:
        raise ValueError(
            "Remote inference requires cluster.enabled and an allowlisted node"
        )
    return endpoint.rstrip("/")


class OpenRouterEngine(_OpenAICompatibleEngine):
    engine_id = "openrouter"
    is_cloud = True
    _default_host = "https://openrouter.ai"
    _api_prefix = "/api/v1"

    def __init__(self, *, trust_env=True) -> None:
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise RuntimeError(
                "Escalation requires OPENROUTER_API_KEY; local state is retained"
            )
        super().__init__(
            host=self._default_host,
            api_key=os.environ["OPENROUTER_API_KEY"],
            trust_env=trust_env,
        )


class SemanticComplexityRouter(InferenceEngine):
    """Keep routine work local; escalate only for explicit depth, size, or retries.

    Context capacity is a configurable budget for the loaded model/KV cache,
    not a claim that GPU VRAM alone determines the context window.
    Cloud construction is lazy. The exact message objects and tool state are
    forwarded on every handoff. Routing metadata never reaches provider APIs.
    """

    engine_id = "desktop"

    def __init__(
        self,
        local: InferenceEngine,
        *,
        local_model: str,
        cloud_model: str = "openrouter/auto",
        local_context_tokens: int = 16384,
        cloud: InferenceEngine | None = None,
        cloud_factory=OpenRouterEngine,
        cloud_enabled: bool = True,
    ) -> None:
        self.local, self.local_model, self.cloud_model = local, local_model, cloud_model
        self.local_context_tokens = local_context_tokens
        self.cloud_enabled = cloud_enabled
        self._cloud, self._cloud_factory = cloud, cloud_factory

    def _select(self, messages: Sequence[Message], kwargs: dict):
        hints = routing_hints.get()
        retries = int(kwargs.pop("retry_count", hints.get("retry_count", 0)))
        depth = kwargs.pop(
            "high_depth_reasoning", hints.get("high_depth_reasoning", False)
        )
        complexity = kwargs.pop("task_complexity", "routine")
        measured = kwargs.pop("context_tokens", None)
        # Include tool schemas and the requested completion in the KV budget.
        tokens = max(int(measured or 0), estimate_prompt_tokens(messages))
        tokens += len(json.dumps(kwargs.get("tools", []))) // 4
        tokens += int(kwargs.get("max_tokens", 1024))
        reason = (
            "context"
            if tokens > self.local_context_tokens
            else "retries"
            if retries >= 2
            else "reasoning"
            if depth or complexity in {"high_depth", "architectural_synthesis"}
            else "local"
        )
        if reason == "local":
            return self.local, self.local_model, reason
        if not self.cloud_enabled:
            if reason == "context":
                raise RuntimeError(
                    "Local context budget exceeded and cloud escalation is disabled"
                )
            return self.local, self.local_model, "local"
        if self._cloud is None:
            self._cloud = self._cloud_factory()
        return self._cloud, self.cloud_model, reason

    def generate(self, messages, *, model: str = "", **kwargs):
        engine, selected, reason = self._select(messages, kwargs)
        result = dict(engine.generate(messages, model=selected, **kwargs))
        result["routing"] = {
            "tier": "local" if engine is self.local else "cloud",
            "reason": reason,
            "model": selected,
        }
        return result

    async def stream(self, messages, *, model: str = "", **kwargs):
        engine, selected, _ = self._select(messages, kwargs)
        async for token in engine.stream(messages, model=selected, **kwargs):
            yield token

    async def stream_full(self, messages, *, model: str = "", **kwargs):
        engine, selected, _ = self._select(messages, kwargs)
        async for chunk in engine.stream_full(messages, model=selected, **kwargs):
            yield chunk

    def list_models(self):
        return [self.local_model]

    def health(self):
        return self.local.health()

    def close(self):
        self.local.close()
        if self._cloud is not None:
            self._cloud.close()


def build_desktop_engine(config):
    from openvarak.engine.ollama import OllamaEngine
    from openvarak.engine.openai_compat_engines import OpenAICompatEngine

    backend = config.engine.default
    endpoints = {
        "ollama": config.engine.ollama_host,
        "vllm": config.engine.vllm_host,
        "sglang": config.engine.sglang_host,
        "llamacpp": config.engine.llamacpp_host,
    }
    if backend not in endpoints:
        raise ValueError("Desktop engine must be ollama, vllm, sglang, or llamacpp")
    defaults = {
        "ollama": "http://127.0.0.1:11434",
        "vllm": "http://127.0.0.1:8000",
        "sglang": "http://127.0.0.1:30000",
        "llamacpp": "http://127.0.0.1:8080",
    }
    host = resolve_endpoint(endpoints[backend] or defaults[backend], config.cluster)
    local = (
        OllamaEngine(host=host, trust_env=False)
        if backend == "ollama"
        else OpenAICompatEngine(host=host, trust_env=False)
    )
    return SemanticComplexityRouter(
        local,
        local_model=config.intelligence.default_model or config.desktop.local_model,
        cloud_model=config.desktop.cloud_model,
        local_context_tokens=config.desktop.local_context_tokens,
        cloud_enabled=config.desktop.cloud_enabled,
    )
