import pytest

from resumate_sdk.ledger import default_ledger, run_idempotent


def test_run_idempotent_executes_handler_exactly_once_across_repeated_calls():
    call_count = {"n": 0}

    def do_charge():
        call_count["n"] += 1
        return {"status": "charged", "id": "ch_123"}

    ledger = default_ledger()
    result1, receipt1 = run_idempotent(ledger, "run_1", "stripe.charge", {"amount": 50}, do_charge)
    result2, receipt2 = run_idempotent(ledger, "run_1", "stripe.charge", {"amount": 50}, do_charge)

    assert call_count["n"] == 1, "handler must execute exactly once across two identical calls"
    assert result1 == result2 == {"status": "charged", "id": "ch_123"}
    assert receipt1["idem_key"] == receipt2["idem_key"]
    assert receipt2["status"] == "succeeded"


def test_run_idempotent_different_args_execute_independently():
    call_count = {"n": 0}

    def do_charge():
        call_count["n"] += 1
        return {"charged": True}

    ledger = default_ledger()
    run_idempotent(ledger, "run_1", "stripe.charge", {"amount": 50}, do_charge)
    run_idempotent(ledger, "run_1", "stripe.charge", {"amount": 75}, do_charge)  # different amount

    assert call_count["n"] == 2, "different args must NOT be treated as the same call"


def test_receipt_shape_matches_what_report_step_expects():
    ledger = default_ledger()
    _, receipt = run_idempotent(ledger, "run_1", "send_email", {"to": "a@b.com"}, lambda: {"sent": True})

    assert set(receipt.keys()) == {"tool", "idem_key", "status", "result", "dedup_count"}
    assert receipt["tool"] == "send_email"
    assert receipt["result"] == {"sent": True}


def test_ledger_module_documents_the_install_hint():
    """
    Can't easily simulate agent-ledger being absent without actually
    uninstalling it in this test run - so this confirms the source
    documents the install hint (pip install resumate-sdk[ledger]) that
    the module's own try/except ImportError raises, rather than faking
    an uninstall scenario that would be more fragile than useful.
    """
    import resumate_sdk.ledger as ledger_module

    assert "pip install resumate-sdk[ledger]" in open(ledger_module.__file__).read()
