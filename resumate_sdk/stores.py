"""
Storage backends for the effect ledger (resumate_sdk.ledger).

Three stores, one small interface:

- MemoryStore    tests only. Lost on restart. Warns when created.
- SQLiteStore    one worker process (one machine). Durable, no server needed.
- PostgresStore  several workers sharing one database.

The ledger relies on exactly two atomic operations from a store:

- insert_if_absent(record): create the entry only if the business key is
  new. This is the "claim first" step.
- cas(key, expected_statuses, **updates): change an entry only if its
  status is still one of the expected ones (compare and set).

Everything else in the ledger is built from those two, so a new store only
has to make them atomic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import warnings
from typing import Any, Callable, Iterable

logger = logging.getLogger("resumate_sdk")

STATUSES = ("processing", "succeeded", "failed", "unknown")

_FIELDS = (
    "business_key",
    "tool",
    "effect_type",
    "status",
    "provider_key",
    "owner",
    "attempts",
    "dedup_count",
    "result",
    "error",
    "created_at",
    "updated_at",
    "claimed_at",
)
_UPDATABLE = frozenset(_FIELDS) - {"business_key", "created_at"}

SELF_CHECK_KEY = "__resumate_selfcheck__"

TABLE = "resumate_effects"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    business_key TEXT PRIMARY KEY,
    tool         TEXT NOT NULL,
    effect_type  TEXT NOT NULL,
    status       TEXT NOT NULL,
    provider_key TEXT NOT NULL,
    owner        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 1,
    dedup_count  INTEGER NOT NULL DEFAULT 0,
    result       TEXT,
    error        TEXT,
    created_at   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL,
    claimed_at   DOUBLE PRECISION NOT NULL
)
"""


def _encode(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _decode(text: str | None) -> Any:
    return None if text is None else json.loads(text)


class MemoryStore:
    """Lost on restart. For tests and demos only."""

    durable = False
    single_worker = True
    name = "memory"

    def __init__(self, *, warn: bool = True):
        if warn:
            warnings.warn(
                "resumate_sdk MemoryStore does not survive a restart: a crash between an effect "
                "happening and the ledger recording it can cause a duplicate on the next start. "
                "Use SQLiteStore or PostgresStore for anything real.",
                RuntimeWarning,
                stacklevel=3,
            )
        self._rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def setup(self) -> None:
        return None

    def insert_if_absent(self, record: dict[str, Any]) -> bool:
        with self._lock:
            if record["business_key"] in self._rows:
                return False
            self._rows[record["business_key"]] = dict(record)
            return True

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._rows.get(key)
            return dict(row) if row else None

    def cas(self, key: str, expected: Iterable[str], **updates: Any) -> bool:
        _check_updates(updates)
        with self._lock:
            row = self._rows.get(key)
            if row is None or row["status"] not in tuple(expected):
                return False
            row.update(updates)
            row["updated_at"] = time.time()
            return True

    def bump_dedup(self, key: str) -> None:
        with self._lock:
            if key in self._rows:
                self._rows[key]["dedup_count"] += 1

    def list(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        wanted = None if statuses is None else tuple(statuses)
        with self._lock:
            rows = [dict(r) for k, r in self._rows.items() if k != SELF_CHECK_KEY]
        return [r for r in rows if wanted is None or r["status"] in wanted]

    def delete(self, key: str) -> None:
        with self._lock:
            self._rows.pop(key, None)


def _check_updates(updates: dict[str, Any]) -> None:
    bad = set(updates) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")


class _SqlStore:
    """Shared SQL for SQLite and Postgres. Subclasses only run statements."""

    placeholder = "?"
    durable = True
    single_worker = True
    name = "sql"

    def _run(self, sql: str, params: tuple, fetch: bool) -> tuple[list[tuple], int]:
        raise NotImplementedError

    def _sql(self, sql: str) -> str:
        return sql.replace("?", self.placeholder)

    def setup(self) -> None:
        self._run(_SCHEMA, (), False)

    def _row_to_dict(self, row: tuple) -> dict[str, Any]:
        rec = dict(zip(_FIELDS, row))
        rec["result"] = _decode(rec["result"])
        return rec

    def insert_if_absent(self, record: dict[str, Any]) -> bool:
        values = dict(record)
        values["result"] = _encode(values.get("result"))
        cols = ", ".join(_FIELDS)
        marks = ", ".join("?" for _ in _FIELDS)
        sql = self._sql(f"INSERT INTO {TABLE} ({cols}) VALUES ({marks}) ON CONFLICT (business_key) DO NOTHING")
        _, count = self._run(sql, tuple(values.get(f) for f in _FIELDS), False)
        return count == 1

    def get(self, key: str) -> dict[str, Any] | None:
        sql = self._sql(f"SELECT {', '.join(_FIELDS)} FROM {TABLE} WHERE business_key = ?")
        rows, _ = self._run(sql, (key,), True)
        return self._row_to_dict(rows[0]) if rows else None

    def cas(self, key: str, expected: Iterable[str], **updates: Any) -> bool:
        _check_updates(updates)
        expected = tuple(expected)
        values = dict(updates)
        if "result" in values:
            values["result"] = _encode(values["result"])
        values["updated_at"] = time.time()
        sets = ", ".join(f"{col} = ?" for col in values)
        marks = ", ".join("?" for _ in expected)
        sql = self._sql(f"UPDATE {TABLE} SET {sets} WHERE business_key = ? AND status IN ({marks})")
        _, count = self._run(sql, (*values.values(), key, *expected), False)
        return count == 1

    def bump_dedup(self, key: str) -> None:
        sql = self._sql(f"UPDATE {TABLE} SET dedup_count = dedup_count + 1 WHERE business_key = ?")
        self._run(sql, (key,), False)

    def list(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {', '.join(_FIELDS)} FROM {TABLE} WHERE business_key <> ?"
        params: tuple = (SELF_CHECK_KEY,)
        if statuses is not None:
            wanted = tuple(statuses)
            sql += f" AND status IN ({', '.join('?' for _ in wanted)})"
            params += wanted
        sql += " ORDER BY created_at"
        rows, _ = self._run(self._sql(sql), params, True)
        return [self._row_to_dict(r) for r in rows]

    def delete(self, key: str) -> None:
        self._run(self._sql(f"DELETE FROM {TABLE} WHERE business_key = ?"), (key,), False)


class SQLiteStore(_SqlStore):
    """
    Durable store for ONE worker process. Several threads in that process
    are fine. Several processes sharing the file work too, but then the
    startup check cannot tell a live process from a dead one: pass
    single_worker=False so only entries older than `stale_after` are
    treated as stuck.
    """

    name = "sqlite"

    def __init__(self, path: str = "resumate_ledger.db", *, single_worker: bool = True):
        self.path = path
        self.single_worker = single_worker
        self.durable = path != ":memory:"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10.0)
        if self.durable:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self.setup()

    def _run(self, sql: str, params: tuple, fetch: bool) -> tuple[list[tuple], int]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if fetch else []
            return rows, cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class PostgresStore(_SqlStore):
    """
    Durable store for several workers sharing one Postgres database. Needs
    `pip install "resumate-sdk[postgres]"` (psycopg 3). Pass either a DSN or
    your own `connection_factory` returning a new psycopg connection.
    Every statement runs in its own short transaction, so the claim and the
    compare-and-set are atomic on the database side.
    """

    placeholder = "%s"
    single_worker = False
    name = "postgres"

    def __init__(self, dsn: str | None = None, *, connection_factory: Callable[[], Any] | None = None):
        if connection_factory is None:
            if dsn is None:
                raise ValueError("PostgresStore needs a dsn or a connection_factory")
            try:
                import psycopg
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ImportError(
                    "PostgresStore needs psycopg 3. Install it with: pip install 'resumate-sdk[postgres]'"
                ) from exc
            connection_factory = lambda: psycopg.connect(dsn)  # noqa: E731
        self._connect = connection_factory
        self.setup()

    def _run(self, sql: str, params: tuple, fetch: bool) -> tuple[list[tuple], int]:
        # `with connection` commits on success, rolls back on error, and (psycopg 3) closes.
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            rows = cur.fetchall() if fetch else []
            return rows, cur.rowcount
