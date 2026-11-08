import asyncio
import threading
import warnings

import pytest

from resumate_sdk import (
    EffectInProgress,
    EffectRejected,
    EffectUnknown,
    Ledger,
    MemoryStore,
    SQLiteStore,
    make_key,
)


def _ledger(**kwargs):
    return Ledger(MemoryStore(warn=False), **kwargs)


def _counter():
    calls = {"n": 0}

    def handler():
        calls["n"] += 1
        return {"id": "ch_1"}

    return calls, handler


def test_handler_runs_once_across_repeated_calls():
    ledger = _ledger()
    calls, handler = _counter()
    key = make_key("stripe.charge", {"order": "o1"})
    r1, rec1 = ledger.run(key, "stripe.charge", handler, effect_type="irreversible")
    r2, rec2 = ledger.run(key, "stripe.charge", handler, effect_type="irreversible")
    assert calls["n"] == 1
    assert r1 == r2 == {"id": "ch_1"}
    assert rec2["status"] == "succeeded"
    assert rec2["dedup_count"] == 1
    assert rec1["idem_key"] == rec2["idem_key"]


def test_receipt_shape():
    ledger = _ledger()
    _, receipt = ledger.run("k", "send_email", lambda: {"sent": True}, effect_type="irreversible")
    assert set(receipt) == {
        "tool", "idem_key", "business_key", "status", "result", "effect_type", "attempts", "dedup_count",
    }
    assert receipt["business_key"] == "k" and receipt["effect_type"] == "irreversible"


def test_different_business_keys_are_independent():
    ledger = _ledger()
    calls, handler = _counter()
    ledger.run(make_key("t", {"o": 1}), "t", handler, effect_type="irreversible")
    ledger.run(make_key("t", {"o": 2}), "t", handler, effect_type="irreversible")
    assert calls["n"] == 2


def test_timeout_becomes_unknown_and_is_never_rerun():
    ledger = _ledger()
    calls = {"n": 0}

    def handler():
        calls["n"] += 1
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError) as info:
        ledger.run("k", "stripe.charge", handler, effect_type="irreversible")
    assert info.value.side_effect_receipt["status"] == "unknown"

    with pytest.raises(EffectUnknown) as again:
        ledger.run("k", "stripe.charge", handler, effect_type="irreversible")
    assert calls["n"] == 1, "an unknown effect must not be re-run by the ledger"
    assert again.value.side_effect_receipt["status"] == "unknown"


def test_clear_rejection_becomes_failed_and_retry_reuses_the_provider_key():
    ledger = _ledger()
    seen = []

    def handler(call):
        seen.append((call.provider_key, call.attempt))
        if call.attempt == 1:
            raise EffectRejected("card declined")
        return {"ok": True}

    with pytest.raises(EffectRejected) as info:
        ledger.run("k", "t", handler, effect_type="irreversible")
    assert info.value.side_effect_receipt["status"] == "failed"
    result, receipt = ledger.run("k", "t", handler, effect_type="irreversible")
    assert result == {"ok": True}
    assert seen[0][0] == seen[1][0], "the same provider key on every retry"
    assert [a for _, a in seen] == [1, 2] and receipt["attempts"] == 2


def test_custom_rejected_exception_types():
    class Declined(Exception):
        pass

    ledger = _ledger(rejected_exceptions=(Declined,))

    def handler():
        raise Declined("no")

    with pytest.raises(Declined) as info:
        ledger.run("k", "t", handler, effect_type="irreversible")
    assert info.value.side_effect_receipt["status"] == "failed"


def test_any_other_exception_is_unknown_not_failed():
    ledger = _ledger()
    with pytest.raises(ValueError) as info:
        ledger.run("k", "t", lambda: (_ for _ in ()).throw(ValueError("boom")), effect_type="reversible")
    assert info.value.side_effect_receipt["status"] == "unknown"


def test_validator_effect_done_returns_result_without_calling_handler():
    ledger = _ledger()
    with pytest.raises(TimeoutError):
        ledger.run("k", "t", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="irreversible")
    calls, handler = _counter()
    result, receipt = ledger.run(
        "k", "t", handler, effect_type="irreversible", validator=lambda: ("effect_done", {"id": "found"})
    )
    assert calls["n"] == 0 and result == {"id": "found"} and receipt["status"] == "succeeded"


def test_validator_effect_absent_runs_the_handler():
    ledger = _ledger()
    with pytest.raises(TimeoutError):
        ledger.run("k", "t", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="irreversible")
    calls, handler = _counter()
    result, receipt = ledger.run("k", "t", handler, effect_type="irreversible", validator=lambda: "effect_absent")
    assert calls["n"] == 1 and receipt["attempts"] == 2


@pytest.mark.parametrize("answer", ["unclear", "maybe", None, 5])
def test_validator_unclear_or_garbage_stays_unknown(answer):
    ledger = _ledger()
    with pytest.raises(TimeoutError):
        ledger.run("k", "t", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="irreversible")
    calls, handler = _counter()
    with pytest.raises(EffectUnknown):
        ledger.run("k", "t", handler, effect_type="irreversible", validator=lambda: answer)
    assert calls["n"] == 0


def test_validator_that_raises_is_unclear():
    ledger = _ledger()
    with pytest.raises(TimeoutError):
        ledger.run("k", "t", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="irreversible")

    def bad():
        raise RuntimeError("lookup down")

    with pytest.raises(EffectUnknown):
        ledger.run("k", "t", lambda: 1, effect_type="irreversible", validator=bad)


def test_repeat_safe_unknown_may_run_again():
    ledger = _ledger()
    with pytest.raises(TimeoutError):
        ledger.run("k", "read", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="repeat_safe")
    result, _ = ledger.run("k", "read", lambda: "fine", effect_type="repeat_safe")
    assert result == "fine"


def test_resolve_by_a_person():
    ledger = _ledger()
    with pytest.raises(TimeoutError):
        ledger.run("k", "t", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="irreversible")
    receipt = ledger.resolve("k", "effect_done", result={"id": 9})
    assert receipt["status"] == "succeeded"
    result, _ = ledger.run("k", "t", lambda: "never", effect_type="irreversible")
    assert result == {"id": 9}
    with pytest.raises(ValueError):
        ledger.resolve("k", "effect_absent")  # already succeeded
    with pytest.raises(ValueError):
        ledger.resolve("k", "whatever")


def test_stuck_processing_after_restart_becomes_unknown_and_is_not_taken_over(tmp_path):
    path = str(tmp_path / "ledger.db")
    first = Ledger(SQLiteStore(path))
    # Simulate a worker that died mid-call: claim, then never finish.
    assert first.store.insert_if_absent(first._new_record("k", "t", "irreversible", 1))
    first.store.close()

    second = Ledger(SQLiteStore(path))
    report = second.startup_check()
    assert report.marked_unknown == ["k"] and report.ok and report.unknown_total == 1
    calls, handler = _counter()
    with pytest.raises(EffectUnknown):
        second.run("k", "t", handler, effect_type="irreversible")
    assert calls["n"] == 0
    text = second.reconciliation_report().to_text()
    assert "k" in text and "need a decision" in text


def test_sqlite_survives_restart_and_dedups(tmp_path):
    path = str(tmp_path / "ledger.db")
    calls, handler = _counter()
    Ledger(SQLiteStore(path)).run("k", "t", handler, effect_type="irreversible")
    again = Ledger(SQLiteStore(path))
    again.startup_check()
    result, receipt = again.run("k", "t", handler, effect_type="irreversible")
    assert calls["n"] == 1 and result == {"id": "ch_1"} and receipt["dedup_count"] == 1


def test_live_processing_entry_of_another_owner_is_in_progress_until_stale():
    clock = {"t": 1000.0}
    store = MemoryStore(warn=False)
    other = Ledger(store, clock=lambda: clock["t"])
    mine = Ledger(store, clock=lambda: clock["t"], stale_after=600)
    store.insert_if_absent(other._new_record("k", "t", "irreversible", 1))
    calls, handler = _counter()
    with pytest.raises(EffectInProgress):
        mine.run("k", "t", handler, effect_type="irreversible")
    clock["t"] += 601
    with pytest.raises(EffectUnknown):  # stale: turned into unknown, never taken over
        mine.run("k", "t", handler, effect_type="irreversible")
    assert calls["n"] == 0


def test_concurrent_callers_with_one_key_run_the_handler_once(tmp_path):
    ledger = Ledger(SQLiteStore(str(tmp_path / "c.db")))
    calls = {"n": 0}
    gate = threading.Event()

    def handler():
        calls["n"] += 1
        gate.wait(2)
        return "done"

    outcomes = []

    def worker():
        try:
            outcomes.append(ledger.run("k", "t", handler, effect_type="irreversible")[0])
        except EffectInProgress:
            outcomes.append("in_progress")

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    threading.Timer(0.3, gate.set).start()
    for t in threads:
        t.join()
    assert calls["n"] == 1
    assert outcomes.count("done") == 1 and outcomes.count("in_progress") == 5


def test_receipt_redacts_secrets_and_error_text_is_cut():
    ledger = _ledger()
    ledger.run("k", "t", lambda: {"id": 1, "api_key": "sk-live-abc", "nested": {"password": "x"}}, effect_type="irreversible")
    receipt = ledger.receipt("k")
    assert receipt["result"]["api_key"] == "[redacted]" and receipt["result"]["nested"]["password"] == "[redacted]"
    assert ledger.get("k")["result"]["api_key"] == "sk-live-abc"  # the local copy is untouched

    def boom():
        raise RuntimeError("Authorization: Bearer abc.def.ghi " + "x " * 400)

    with pytest.raises(RuntimeError):
        ledger.run("e", "t", boom, effect_type="irreversible")
    error = ledger.get("e")["error"]
    assert "abc.def.ghi" not in error and len(error) <= 300


def test_receipt_can_leave_the_result_out():
    ledger = _ledger(receipt_includes_result=False)
    _, receipt = ledger.run("k", "t", lambda: {"secret_thing": 1}, effect_type="irreversible")
    assert receipt["result"] is None and ledger.get("k")["result"] == {"secret_thing": 1}


def test_make_key_uses_the_business_object_and_item_index():
    a = make_key("t", {"order": "o1", "amount": 5})
    assert a == make_key("t", {"amount": 5, "order": "o1"})
    assert a != make_key("t", {"order": "o2", "amount": 5})
    assert make_key("t", {"order": "o1"}, 0) != make_key("t", {"order": "o1"}, 1)


def test_bad_effect_type_is_rejected():
    with pytest.raises(ValueError):
        _ledger().run("k", "t", lambda: 1, effect_type="maybe")


def test_startup_check_warns_for_memory_and_reports_a_broken_store():
    assert any("not durable" in w for w in _ledger().startup_check().warnings)

    class Broken(MemoryStore):
        def insert_if_absent(self, record):
            raise OSError("disk full")

    report = Ledger(Broken(warn=False)).startup_check()
    assert not report.ok and "disk full" in report.problems[0]


def test_memory_store_warns_when_created():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Ledger()
    assert any("does not survive a restart" in str(w.message) for w in caught)


def test_async_run_with_async_handler_and_validator():
    async def scenario():
        ledger = _ledger()
        calls = {"n": 0}

        async def handler():
            calls["n"] += 1
            return {"id": 1}

        r1, _ = await ledger.arun("k", "t", handler, effect_type="irreversible")
        r2, rec = await ledger.arun("k", "t", handler, effect_type="irreversible")
        assert calls["n"] == 1 and r1 == r2 and rec["dedup_count"] == 1

        async def timeout():
            raise TimeoutError()

        with pytest.raises(TimeoutError):
            await ledger.arun("u", "t", timeout, effect_type="irreversible")

        async def validator():
            return "effect_absent"

        r3, rec3 = await ledger.arun("u", "t", handler, effect_type="irreversible", validator=validator)
        assert r3 == {"id": 1} and rec3["attempts"] == 2

    asyncio.run(scenario())
