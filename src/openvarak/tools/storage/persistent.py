"""Python SQLite FTS5 memory with a durable, ordered Supabase outbox."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from openvarak.core.paths import get_config_dir
from openvarak.core.registry import MemoryRegistry
from openvarak.tools.storage._stubs import MemoryBackend, RetrievalResult

logger = logging.getLogger(__name__)


@MemoryRegistry.register("hybrid_persistent")
class HybridPersistentMemoryBackend(MemoryBackend):
    """Local commits never wait on embedding or cloud IO.

    FTS indexes are maintained transactionally by SQLite triggers. The outbox
    commits in the same transaction as each mutation and survives restarts.
    Supabase sync is at-least-once and uses idempotent row upserts/deletes.
    SQLite remains authoritative; credentials and connectivity are optional.
    """

    backend_id = "hybrid_persistent"

    def __init__(
        self,
        db_path="",
        *,
        supabase_url="",
        supabase_key="",
        sync_enabled=False,
        sync_interval=5.0,
        embedding_fn=None,
        transport=None,
    ):
        self._db_path = str(
            Path(db_path or get_config_dir() / "memory.db").expanduser()
        )
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS varak_documents (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT NOT NULL,
                metadata TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS varak_fts USING fts5(
                content, source, content='varak_documents', content_rowid='rowid',
                tokenize='porter unicode61');
            CREATE TRIGGER IF NOT EXISTS varak_insert AFTER INSERT ON varak_documents
            BEGIN INSERT INTO varak_fts(rowid,content,source)
                VALUES(new.rowid,new.content,new.source); END;
            CREATE TRIGGER IF NOT EXISTS varak_delete AFTER DELETE ON varak_documents
            BEGIN INSERT INTO varak_fts(varak_fts,rowid,content,source)
                VALUES('delete',old.rowid,old.content,old.source); END;
            CREATE TABLE IF NOT EXISTS varak_outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        """)
        self._url, self._key = supabase_url.rstrip("/"), supabase_key
        self._interval = max(0.1, sync_interval)
        self._embedding_fn, self._transport = embedding_fn, transport
        self._stop, self._wake = threading.Event(), threading.Event()
        self._sync_lock = threading.Lock()
        self._worker = None
        if sync_enabled and self._url and self._key:
            self._worker = threading.Thread(
                target=self._sync_loop, daemon=True, name="varak-memory-sync"
            )
            self._worker.start()

    @classmethod
    def from_config(cls, config):
        storage = config.memory
        embedding_fn = None
        if storage.supabase_enabled:
            from openvarak.engine.desktop import resolve_endpoint

            endpoint = resolve_endpoint(config.engine.ollama_host, config.cluster)

            def embedding_fn(content):
                import httpx

                response = httpx.post(
                    f"{endpoint}/api/embed",
                    timeout=20,
                    json={"model": "nomic-embed-text", "input": content},
                )
                response.raise_for_status()
                return response.json()["embeddings"][0]

        return cls(
            storage.db_path,
            supabase_url=os.environ.get(storage.supabase_url_env, ""),
            supabase_key=os.environ.get(storage.supabase_key_env, ""),
            sync_enabled=storage.supabase_enabled,
            sync_interval=storage.sync_interval_seconds,
            embedding_fn=embedding_fn,
        )

    def _enqueue(self, payload):
        self._conn.execute(
            "INSERT INTO varak_outbox(payload) VALUES (?)", (json.dumps(payload),)
        )

    def store(self, content, *, source="", metadata=None):
        doc = {
            "id": uuid.uuid4().hex,
            "content": content,
            "source": source,
            "metadata": metadata or {},
            "created_at": time.time(),
        }
        with self._lock, self._conn:
            self._insert(doc)
        self._wake.set()
        return doc["id"]

    def _insert(self, doc):
        self._conn.execute(
            "INSERT INTO varak_documents VALUES (?,?,?,?,?)",
            (
                doc["id"],
                doc["content"],
                doc["source"],
                json.dumps(doc["metadata"]),
                doc["created_at"],
            ),
        )
        self._enqueue({"operation": "upsert", "document": doc})

    def replace_source(self, source, documents):
        ids = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id FROM varak_documents WHERE source=?", (source,)
            ).fetchall()
            for (doc_id,) in rows:
                self._delete(doc_id)
            for content, metadata in documents:
                doc = {
                    "id": uuid.uuid4().hex,
                    "content": content,
                    "source": source,
                    "metadata": metadata or {},
                    "created_at": time.time(),
                }
                self._insert(doc)
                ids.append(doc["id"])
        self._wake.set()
        return ids

    def retrieve(self, query, *, top_k=5, **kwargs):
        terms = re.findall(r"\w+", query, flags=re.UNICODE)
        if not terms or top_k <= 0:
            return []
        expression = " OR ".join('"' + term + '"' for term in terms)
        with self._lock:
            rows = self._conn.execute(
                """SELECT d.content, d.source, d.metadata,
                bm25(varak_fts) FROM varak_fts JOIN varak_documents d
                ON d.rowid=varak_fts.rowid WHERE varak_fts MATCH ?
                ORDER BY bm25(varak_fts) LIMIT ?""",
                (expression, top_k),
            ).fetchall()
        return [
            RetrievalResult(content=c, source=s, metadata=json.loads(m), score=-score)
            for c, s, m, score in rows
        ]

    def _delete(self, doc_id):
        cursor = self._conn.execute("DELETE FROM varak_documents WHERE id=?", (doc_id,))
        if cursor.rowcount:
            self._enqueue({"operation": "delete", "id": doc_id})
        return bool(cursor.rowcount)

    def delete(self, doc_id):
        with self._lock, self._conn:
            deleted = self._delete(doc_id)
        self._wake.set()
        return deleted

    def clear(self):
        with self._lock, self._conn:
            for (doc_id,) in self._conn.execute(
                "SELECT id FROM varak_documents"
            ).fetchall():
                self._delete(doc_id)
        self._wake.set()

    def count(self):
        with self._lock:
            return self._conn.execute(
                "SELECT count(*) FROM varak_documents"
            ).fetchone()[0]

    def pending_writes(self):
        with self._lock:
            return self._conn.execute("SELECT count(*) FROM varak_outbox").fetchone()[0]

    def checkpoint(self, agent_id, state, *, kind="checkpoint"):
        return self.store(
            json.dumps(state),
            source=f"{kind}:{agent_id}",
            metadata={"kind": kind, "agent_id": agent_id},
        )

    def latest_checkpoint(self, agent_id):
        with self._lock:
            row = self._conn.execute(
                """SELECT content FROM varak_documents
                WHERE source=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (f"checkpoint:{agent_id}",),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def sync_once(self, batch_size=32):
        """Flush in order outside the SQLite lock; retain failures for retry."""
        if not self._url or not self._key:
            return 0
        import httpx

        flushed = 0
        with (
            self._sync_lock,
            httpx.Client(timeout=10, transport=self._transport) as client,
        ):
            with self._lock:
                rows = self._conn.execute(
                    "SELECT sequence,payload FROM varak_outbox "
                    "ORDER BY sequence LIMIT ?",
                    (batch_size,),
                ).fetchall()
            for seq, raw in rows:
                if self._stop.is_set():
                    break
                payload = json.loads(raw)
                headers = {
                    "apikey": self._key,
                    "Authorization": f"Bearer {self._key}",
                    "Prefer": "resolution=merge-duplicates",
                }
                url = f"{self._url}/rest/v1/varak_memory"
                if payload["operation"] == "upsert":
                    doc = payload["document"]
                    if self._embedding_fn:
                        doc["embedding"] = self._embedding_fn(doc["content"])
                    response = client.post(url, headers=headers, json=doc)
                else:
                    response = client.delete(
                        url, headers=headers, params={"id": f"eq.{payload['id']}"}
                    )
                response.raise_for_status()
                with self._lock, self._conn:
                    self._conn.execute(
                        "DELETE FROM varak_outbox WHERE sequence=?", (seq,)
                    )
                flushed += 1
        return flushed

    def semantic_retrieve(self, embedding, *, top_k=5):
        """Explicit optional cloud search; never called by local FTS retrieval."""
        if not self._url or not self._key:
            return []
        import httpx

        response = httpx.post(
            f"{self._url}/rest/v1/rpc/varak_match_memory",
            timeout=10,
            headers={"apikey": self._key, "Authorization": f"Bearer {self._key}"},
            json={"query_embedding": embedding, "match_count": top_k},
        )
        response.raise_for_status()
        return [
            RetrievalResult(
                content=r["content"],
                source=r["source"],
                metadata=r["metadata"],
                score=r["score"],
            )
            for r in response.json()
        ]

    def _sync_loop(self):
        while not self._stop.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            try:
                while self.sync_once() == 32 and not self._stop.is_set():
                    pass
            except Exception:
                # Do not log response bodies or credentials.
                logger.warning(
                    "Supabase sync unavailable; writes remain queued locally"
                )

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._worker:
            self._worker.join()  # network operations have bounded timeouts
        with self._lock:
            self._conn.close()
