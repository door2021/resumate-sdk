import httpx
import pytest

from resumate_sdk import LangGraphRunTracker, ResumateAPIRejected, ResumateClient
from resumate_sdk.exceptions import ResumateAPIError


def _client(handler, **kwargs):
    return ResumateClient(api_key="k", base_url="http://test", transport=httpx.MockTransport(handler), **kwargs)


def test_transient_failures_recover_via_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={"step_recorded": True, "run_status": "running", "resume": {}})

    client = _client(handler, backoff_base=0.001, backoff_max=0.005)
    result = client.report_step(agent_name="a", run_external_id="r1", step_index=0, step_name="s", status="succeeded")

    assert calls["n"] == 3
    assert result["step_recorded"] is True


def test_fail_open_default_does_not_crash_a_successful_node():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler, backoff_base=0.001, backoff_max=0.002, max_retries=1)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="r2")

    @tracker.track("does_real_work")
    def node(state):
        return {"ok": True, "important_result": 42}

    # Must NOT raise, even though the API is fully unreachable - a
    # Resumate outage must never crash a customer's successful step.
    result = node({})
    assert result == {"ok": True, "important_result": 42}


def test_original_exception_always_wins_even_in_strict_fail_open_false_mode():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler, backoff_base=0.001, backoff_max=0.002, max_retries=1, fail_open=False)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="r3")

    class MyAgentBug(Exception):
        pass

    @tracker.track("buggy_node")
    def node(state):
        raise MyAgentBug("the agent's own real bug")

    # Even in the strict mode where reporting failures normally raise,
    # the NODE's own exception must be what propagates - a reporting
    # failure must never mask a real agent bug.
    with pytest.raises(MyAgentBug):
        node({})


def test_non_retryable_error_fails_fast_without_wasting_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "invalid or missing API key"})

    client = _client(handler, max_retries=5, fail_open=False)

    with pytest.raises(ResumateAPIRejected):
        client.get_resume("some_run")

    assert calls["n"] == 1, "a 401 must not be retried - retrying a bad API key 5 times is pointless"


def test_fail_open_false_raises_resumate_api_error_on_success_path_when_api_down():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler, backoff_base=0.001, backoff_max=0.002, max_retries=1, fail_open=False)

    with pytest.raises(ResumateAPIError):
        client.report_step(agent_name="a", run_external_id="r4", step_index=0, step_name="s", status="succeeded")


def test_resume_from_last_failure_sets_step_index_and_consumes_pending_repair():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return httpx.Response(200, json={
                "run_status": "repairing",
                "resume": {"resume_from_step": 2, "repaired_input": {"timeout_seconds": 30}},
            })
        return httpx.Response(200, json={"consumed": True})

    client = _client(handler)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="r5")

    repaired_input = tracker.resume_from_last_failure()

    assert repaired_input == {"timeout_seconds": 30}
    assert tracker._step_index == 2, "next .track()-wrapped call must report step_index=2, not 0"
    methods_called = [m for m, _ in calls]
    assert methods_called == ["GET", "POST"], "a pending repair must be automatically consumed"


def test_resume_from_last_failure_does_not_consume_when_nothing_pending():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={
            "run_status": "running",
            "resume": {"resume_from_step": 3, "repaired_input": None},
        })

    client = _client(handler)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="r6")

    repaired_input = tracker.resume_from_last_failure()

    assert repaired_input is None
    assert tracker._step_index == 3
    assert calls == ["GET"], "must not call consume when there's nothing to consume"
