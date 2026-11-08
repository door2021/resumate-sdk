"""
Store tests. PostgresStore is exercised through a FAKE connection that runs the
same SQL on SQLite (translating psycopg's %s placeholders), so these tests prove
the store's logic and its use of the connection API, NOT Postgres itself.
The last test here runs against a real Postgres when RESUMATE_TEST_POSTGRES_DSN is set.
"""

import os
import sqlite3

import pytest

from resumate_sdk import Ledger, MemoryStore, PostgresStore, SQLiteStore


class _FakeConn:
    def __init__(self, path):
        self._c = sqlite3.connect(path, isolation_level=None, timeout=10)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._c.close()

    def execute(self, sql, params=()):
        assert "?" not in sql, "postgres statements must use %s placeholders"
        return self._c.execute(sql.replace("%s", "?"), params)


def _stores(tmp_path):
    path = str(tmp_path / "fake_pg.db")
    return [
        MemoryStore(warn=False),
        SQLiteStore(str(tmp_path / "s.db")),
        PostgresStore(connection_factory=lambda: _FakeConn(path)),
    ]


def _record(key="k", status="processing"):
    return {
        "business_key": key, "tool": "t", "effect_type": "irreversible", "status": status,
        "provider_key": "rsm_x", "owner": "o1", "attempts": 1, "dedup_count": 0, "result": None,
        "error": None, "created_at": 1.0, "updated_at": 1.0, "claimed_at": 1.0,
    }


def test_every_store_has_an_atomic_claim_and_compare_and_set(tmp_path):
    for store in _stores(tmp_path):
        assert store.insert_if_absent(_record()) is True
        assert store.insert_if_absent(_record()) is False, store.name
        assert store.cas("k", ["succeeded"], status="failed") is False
        assert store.cas("k", ["processing"], status="succeeded", result={"a": [1, 2]}) is True
        assert store.cas("k", ["processing"], status="failed") is False
        rec = store.get("k")
        assert rec["status"] == "succeeded" and rec["result"] == {"a": [1, 2]}
        store.bump_dedup("k")
        assert store.get("k")["dedup_count"] == 1
        assert [r["business_key"] for r in store.list(["succeeded"])] == ["k"]
        assert store.list(["unknown"]) == []
        store.delete("k")
        assert store.get("k") is None


def test_cas_refuses_unknown_fields(tmp_path):
    for store in _stores(tmp_path):
        store.insert_if_absent(_record())
        with pytest.raises(ValueError):
            store.cas("k", ["processing"], business_key="other")


def test_ledger_behaves_the_same_on_every_store(tmp_path):
    for store in _stores(tmp_path):
        ledger = Ledger(store)
        calls = {"n": 0}

        def handler():
            calls["n"] += 1
            return {"id": 1}

        ledger.run("k", "t", handler, effect_type="irreversible")
        ledger.run("k", "t", handler, effect_type="irreversible")
        assert calls["n"] == 1, store.name

        def timeout():
            raise TimeoutError()

        with pytest.raises(TimeoutError):
            ledger.run("u", "t", timeout, effect_type="irreversible")
        assert [e["business_key"] for e in ledger.reconciliation_report().entries] == ["u"], store.name
        report = ledger.startup_check()
        assert report.ok, store.name


def test_postgres_store_needs_a_dsn_or_factory():
    with pytest.raises(ValueError):
        PostgresStore()


def test_sqlite_memory_path_is_reported_as_not_durable():
    assert SQLiteStore(":memory:").durable is False
    assert Ledger(SQLiteStore(":memory:")).startup_check().warnings


@pytest.mark.skipif(not os.environ.get("RESUMATE_TEST_POSTGRES_DSN"), reason="set RESUMATE_TEST_POSTGRES_DSN to run against a real Postgres")
def test_postgres_live_claim_is_atomic_across_connections():
    import threading

    store = PostgresStore(os.environ["RESUMATE_TEST_POSTGRES_DSN"])
    key = f"live-{os.getpid()}"
    store.delete(key)
    results = []
    threads = [threading.Thread(target=lambda: results.append(store.insert_if_absent(_record(key)))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
    ledger = Ledger(store)
    assert ledger.startup_check().ok
    store.delete(key)
