"""Storage primitive — persistent searchable storage."""

from __future__ import annotations

import openvarak.tools.storage.persistent  # noqa: F401

# Always-available backend
import openvarak.tools.storage.sqlite  # noqa: F401
from openvarak.tools.storage._stubs import MemoryBackend, RetrievalResult
from openvarak.tools.storage.chunking import Chunk, ChunkConfig, chunk_text
from openvarak.tools.storage.context import ContextConfig, inject_context
from openvarak.tools.storage.ingest import ingest_path, read_document

__all__ = [
    "Chunk",
    "ChunkConfig",
    "ContextConfig",
    "MemoryBackend",
    "RetrievalResult",
    "chunk_text",
    "create_memory_backend",
    "inject_context",
    "ingest_path",
    "read_document",
]


_BACKEND_MODULES = {
    "sqlite": ("sqlite", "SQLiteMemory"),
    "hybrid_persistent": ("persistent", "HybridPersistentMemoryBackend"),
    "bm25": ("bm25", "BM25Memory"),
    "faiss": ("faiss_backend", "FAISSMemory"),
    "colbert": ("colbert_backend", "ColBERTMemory"),
    "hybrid": ("hybrid", "HybridMemory"),
    "dense": ("dense", "DenseMemory"),
    "knowledge": ("openvarak.connectors.store", "KnowledgeStore"),
    "knowledge_graph": ("knowledge_graph", "KnowledgeGraphMemory"),
}


def create_memory_backend(config, backend_key=None):
    """Resolve a configured backend consistently across CLI, SDK, and server.

    Optional vector backends load only when selected. Re-registering a cached
    built-in also supports long-lived hosts that reset their component registry.
    """
    import importlib
    import inspect

    from openvarak.core.registry import MemoryRegistry

    key = backend_key or config.memory.default_backend
    if not MemoryRegistry.contains(key) and key in _BACKEND_MODULES:
        module, name = _BACKEND_MODULES[key]
        module_path = (
            module
            if module.startswith("openvarak.")
            else (f"openvarak.tools.storage.{module}")
        )
        imported = importlib.import_module(module_path)
        if not MemoryRegistry.contains(key):
            MemoryRegistry.register_value(key, getattr(imported, name))
    cls = MemoryRegistry.get(key)
    if key == "hybrid_persistent":
        return cls.from_config(config)
    try:
        accepts_path = "db_path" in inspect.signature(cls).parameters
    except (TypeError, ValueError):
        accepts_path = False
    if accepts_path:
        return cls(db_path=config.memory.db_path)
    return cls()
