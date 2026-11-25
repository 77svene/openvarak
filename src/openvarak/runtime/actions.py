"""Persistent action receipts, reservations and settlement accounting.

SQLite is the dependency-free default. PostgreSQL uses the same contract and
serializes budget decisions across processes. Neither backend authorizes an
external action; existing capability/confirmation checks still apply.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from openvarak.core.types import ToolResult

action_job: ContextVar[str] = ContextVar("varak_action_job", default="")
action_deadline: ContextVar[float | None] = ContextVar(
    "varak_action_deadline", default=None
)


class ActionBlocked(RuntimeError):
    """An unresolved action, changed receipt or exhausted budget blocks work."""


def is_mutating(tool, params):
    """Unknown tools are consequential; tool authors may explicitly mark reads."""
    declared = tool.spec.metadata.get("side_effects")
    if isinstance(declared, bool):
        return declared
    if tool.spec.name in {
        "calculator",
        "file_read",
        "memory_retrieve",
        "web_search",
        "memory_search",
        "read_file",
    }:
        return False
    if tool.spec.name == "playwright_session":
        return params.get("action") not in {
            "navigate",
            "get_text",
            "get_html",
            "screenshot",
            "tabs",
            "snapshot",
        }
    return True


class ActionJournal:
    def __init__(self, target, *, backend="sqlite", budget_minor=0, currency="CAD"):
        if type(budget_minor) is not int or budget_minor < 0:
            raise ValueError("action_budget_minor must be a nonnegative integer")
        if not isinstance(currency, str) or len(currency) != 3:
            raise ValueError("An explicit three-letter currency is required")
        self.backend = backend
        self.target = target
        self._lock = threading.RLock()
        if backend == "sqlite":
            self.db = sqlite3.connect(
                str(target), check_same_thread=False, isolation_level=None, timeout=10
            )
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        elif backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            self.db = psycopg.connect(target, autocommit=True, row_factory=dict_row)
        else:
            raise ValueError("action_backend must be sqlite or postgres")
        statements = [
            """CREATE TABLE IF NOT EXISTS varak_action_budget (
                scope TEXT PRIMARY KEY, limit_minor BIGINT NOT NULL,
                currency TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS varak_actions (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, tool TEXT NOT NULL,
                args_json TEXT NOT NULL, cost_minor BIGINT NOT NULL,
                currency TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
                created_at DOUBLE PRECISION NOT NULL,
                updated_at DOUBLE PRECISION NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS varak_actions_job ON varak_actions(job_id)",
            """CREATE TABLE IF NOT EXISTS varak_action_events (
                id TEXT PRIMARY KEY, action_id TEXT NOT NULL, state TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS varak_settlements (
                receipt_id TEXT PRIMARY KEY, action_id TEXT NOT NULL,
                revenue_minor BIGINT NOT NULL, cost_minor BIGINT NOT NULL,
                currency TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS varak_ledger (
                receipt_id TEXT NOT NULL, account TEXT NOT NULL,
                currency TEXT NOT NULL, debit_minor BIGINT NOT NULL,
                credit_minor BIGINT NOT NULL, PRIMARY KEY(receipt_id, account))""",
        ]
        try:
            for statement in statements:
                self._sql(statement)
            self._sql(
                "INSERT INTO varak_action_budget VALUES ('desktop',?,?) "
                "ON CONFLICT(scope) DO NOTHING",
                (budget_minor, currency),
            )
            row = self._sql(
                "SELECT * FROM varak_action_budget WHERE scope='desktop'"
            ).fetchone()
            if row["currency"] != currency or row["limit_minor"] != budget_minor:
                raise ValueError(
                    "Stored action budget differs; use set_budget explicitly"
                )
        except BaseException:
            self.db.close()
            raise

    def _sql(self, statement, params=()):
        return self.db.execute(
            statement.replace("?", "%s") if self.backend == "postgres" else statement,
            params,
        )

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._sql("BEGIN IMMEDIATE" if self.backend == "sqlite" else "BEGIN")
            try:
                # All mutations lock the same budget row, including receipt writes.
                suffix = " FOR UPDATE" if self.backend == "postgres" else ""
                self._sql(
                    "SELECT * FROM varak_action_budget WHERE scope='desktop'" + suffix
                )
                yield
                self._sql("COMMIT")
            except BaseException:
                self._sql("ROLLBACK")
                raise

    def _event(self, action_id, state):
        self._sql(
            "INSERT INTO varak_action_events VALUES (?,?,?,?)",
            (uuid.uuid4().hex, action_id, state, time.time()),
        )

    def claim(self, job_id, tool, params, *, cost_minor=0):
        if not job_id:
            raise ActionBlocked("A durable job identity is required")
        if type(cost_minor) is not int or cost_minor < 0:
            raise ValueError("Tool cost must be a nonnegative integer")
        args = json.dumps(
            params, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        key = hashlib.sha256(json.dumps([job_id, tool, args]).encode()).hexdigest()
        with self._transaction():
            old = self._sql("SELECT * FROM varak_actions WHERE id=?", (key,)).fetchone()
            if old:
                if old["cost_minor"] != cost_minor:
                    raise ActionBlocked("A reserved action's cost cannot change")
                if old["state"] != "succeeded":
                    raise ActionBlocked(
                        "Action outcome unresolved; reconcile before replay"
                    )
                saved = json.loads(old["result_json"])
                result = ToolResult(**saved)
                result.metadata.update(action_id=key, journal_replay=True)
                return key, result
            deadline = action_deadline.get()
            if deadline is not None and time.time() >= deadline:
                raise ActionBlocked("Action deadline expired")
            if self._sql(
                "SELECT id FROM varak_actions WHERE job_id=? "
                "AND state IN ('running','unknown')",
                (job_id,),
            ).fetchone():
                raise ActionBlocked("This job has an unresolved action")
            budget = self._sql(
                "SELECT * FROM varak_action_budget WHERE scope='desktop'"
            ).fetchone()
            used = self._sql(
                "SELECT COALESCE(SUM(cost_minor),0) AS used FROM varak_actions"
            ).fetchone()["used"]
            if used + cost_minor > budget["limit_minor"]:
                raise ActionBlocked("Action budget exceeded")
            now = time.time()
            self._sql(
                "INSERT INTO varak_actions VALUES (?,?,?,?,?,?,'running',NULL,?,?)",
                (key, job_id, tool, args, cost_minor, budget["currency"], now, now),
            )
            self._event(key, "running")
        return key, None

    def finish(self, key, result):
        state = "succeeded" if result.success else "unknown"
        # Internal taint objects stay in memory; receipts contain JSON-safe data.
        encoded = json.dumps(
            {
                "tool_name": result.tool_name,
                "content": result.content,
                "success": result.success,
                "metadata": {"action_id": key},
            },
            default=str,
        )
        with self._transaction():
            row = self._sql("SELECT * FROM varak_actions WHERE id=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if result.tool_name != row["tool"]:
                raise ActionBlocked("Receipt belongs to a different tool")
            if row["state"] == "succeeded":
                return
            self._sql(
                "UPDATE varak_actions SET state=?,result_json=?,updated_at=? "
                "WHERE id=?",
                (state, encoded, time.time(), key),
            )
            self._event(key, state)

    def unknown(self, key):
        with self._transaction():
            row = self._sql(
                "SELECT state FROM varak_actions WHERE id=?", (key,)
            ).fetchone()
            if row and row["state"] == "running":
                self._sql(
                    "UPDATE varak_actions SET state='unknown',updated_at=? WHERE id=?",
                    (time.time(), key),
                )
                self._event(key, "unknown")

    def recover(self, job_ids):
        """Caller must hold the runtime lease for these jobs."""
        for job_id in job_ids:
            for row in self.actions(job_id):
                if row["state"] == "running":
                    self.unknown(row["id"])

    def actions(self, job_id):
        with self._lock:
            return [
                dict(row)
                for row in self._sql(
                    "SELECT * FROM varak_actions WHERE job_id=? ORDER BY created_at,id",
                    (job_id,),
                ).fetchall()
            ]

    def unresolved(self, job_id):
        return any(
            row["state"] in {"running", "unknown"} for row in self.actions(job_id)
        )

    def reconcile(self, key, lookup):
        with self._lock:
            row = self._sql("SELECT * FROM varak_actions WHERE id=?", (key,)).fetchone()
            if not row:
                raise KeyError(key)
            row = dict(row)
        if row["state"] == "succeeded":
            return ToolResult(**json.loads(row["result_json"]))
        if row["state"] != "unknown":
            raise ActionBlocked(
                "Confirm the original worker stopped before reconciliation"
            )
        result = lookup(row["tool"], json.loads(row["args_json"]))
        if not isinstance(result, ToolResult) or not result.success:
            raise ActionBlocked(
                "Authoritative reconciliation did not establish success"
            )
        if result.tool_name != row["tool"]:
            raise ActionBlocked("Receipt belongs to a different tool")
        self.finish(key, result)
        return result

    def set_budget(self, limit_minor):
        """Administrative operation; never exposed as a model tool."""
        if type(limit_minor) is not int or limit_minor < 0:
            raise ValueError("Budget must be a nonnegative integer")
        with self._transaction():
            used = self._sql(
                "SELECT COALESCE(SUM(cost_minor),0) AS used FROM varak_actions"
            ).fetchone()["used"]
            if limit_minor < used:
                raise ActionBlocked("Cannot lower budget below committed cost")
            self._sql(
                "UPDATE varak_action_budget SET limit_minor=? WHERE scope='desktop'",
                (limit_minor,),
            )

    def record_settlement(
        self, receipt_id, action_id, *, revenue_minor, cost_minor, currency
    ):
        """Trusted payment adapters call this with settled external receipts."""
        if not receipt_id or any(
            type(x) is not int or x < 0 for x in (revenue_minor, cost_minor)
        ):
            raise ValueError("Receipt and nonnegative integer amounts required")
        with self._transaction():
            action = self._sql(
                "SELECT * FROM varak_actions WHERE id=?", (action_id,)
            ).fetchone()
            if not action or action["state"] != "succeeded":
                raise ActionBlocked("A successful action receipt is required")
            if currency != action["currency"] or cost_minor > action["cost_minor"]:
                raise ActionBlocked(
                    "Settlement exceeds reserved cost or changes currency"
                )
            values = (receipt_id, action_id, revenue_minor, cost_minor, currency)
            old = self._sql(
                "SELECT * FROM varak_settlements WHERE receipt_id=?", (receipt_id,)
            ).fetchone()
            if old:
                if (
                    tuple(
                        old[k]
                        for k in (
                            "receipt_id",
                            "action_id",
                            "revenue_minor",
                            "cost_minor",
                            "currency",
                        )
                    )
                    != values
                ):
                    raise ActionBlocked("Settlement receipt changed")
                return
            self._sql("INSERT INTO varak_settlements VALUES (?,?,?,?,?)", values)
            for account, debit, credit in [
                ("cash", revenue_minor, cost_minor),
                ("income", 0, revenue_minor),
                ("expense", cost_minor, 0),
            ]:
                self._sql(
                    "INSERT INTO varak_ledger VALUES (?,?,?,?,?)",
                    (receipt_id, account, currency, debit, credit),
                )

    def totals(self):
        with self._lock:
            row = self._sql(
                "SELECT COALESCE(SUM(revenue_minor),0) AS revenue_minor, "
                "COALESCE(SUM(cost_minor),0) AS cost_minor FROM varak_settlements"
            ).fetchone()
            result = dict(row)
            result["contribution_minor"] = (
                result["revenue_minor"] - result["cost_minor"]
            )
            result["committed_minor"] = self._sql(
                "SELECT COALESCE(SUM(cost_minor),0) AS used FROM varak_actions"
            ).fetchone()["used"]
            result["reserved_minor"] = self._sql(
                "SELECT COALESCE(SUM(cost_minor),0) AS used FROM varak_actions "
                "WHERE state IN ('running','unknown')"
            ).fetchone()["used"]
            result["currency"] = self._sql(
                "SELECT currency FROM varak_action_budget WHERE scope='desktop'"
            ).fetchone()["currency"]
            return result

    def close(self):
        with self._lock:
            self.db.close()
