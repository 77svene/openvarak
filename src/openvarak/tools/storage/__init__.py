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
    "inject_context",
    "ingest_path",
    "read_document",
]
