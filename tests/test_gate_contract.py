"""
The SDK's side of the server's recovery gate: what it sends (effect_type,
validator_registered, receipts) and how it reacts to what the server answers
(revalidate, skip, confirmed, hold).
"""

import json
from typing import TypedDict

import httpx
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from resumate_sdk import LangGraphRunTracker, Ledger, MemoryStore, ResumateClient


class Server:
    """A tiny fake Resumate server that records requests and replays scripted resume answers."""

    def __init__(self):
        self.requests = []
        self.resume_answers = []  # popped per GET /resume/
        self.revalidation_answer = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/revalidation/"):
            return httpx.Response(200, json=self.revalidation_answer)
        if request.url.path.endswith("/resume/") and request.method == "GET":
            return httpx.Response(200, json=self.resume_answers.pop(0))
        if request.url.path.endswith("/consume/"):
            return httpx.Response(200, json={"consumed": True})
        return httpx.Response(200, json={"step_recorded": True, "run_status": "failed", "resume": {}})

    def posted(self, suffix):
        return [b for m, p, b in self.requests if m == "POST" and p.endswith(suffix)]


def _tracker(server, **kw):
    client = ResumateClient(api_key="k", base_url="http://t", transport=httpx.MockTransport(server))
    return LangGraphRunTracker(client, agent_name="a", run_id="r1", **kw)


def _ledger():
    return Ledger(MemoryStore(warn=False))


def test_failed_and_succeeded_steps_send_effect_type_and_validator_flag():
    server = Server()
    tracker = _tracker(server)

    @tracker.track("charge", effect_type="irreversible", validator=lambda: "unclear")
    def charge(state):
        return {"ok": 1}

    @tracker.track("read", effect_type="repeat_safe")
    def read(state):
        raise TimeoutError("slow")

    charge({})
    with pytest.raises(TimeoutError):
        read({})
    first, second = server.posted("/checkpoints/")
    assert first["effect_type"] == "irreversible" and first["validator_registered"] is True
    assert second["effect_type"] == "repeat_safe" and second["validator_registered"] is False


def test_undeclared_steps_send_neither_field():
    server = Server()
    tracker = _tracker(server)
    tracker.track("plain")(lambda s: {"a": 1})({})
    assert "effect_type" not in server.posted("/checkpoints/")[0]
    assert "validator_registered" not in server.posted("/checkpoints/")[0]


def test_bad_effect_type_is_refused_at_decoration_time():
    with pytest.raises(ValueError):
        _tracker(Server()).track("x", effect_type="sometimes")


def test_unknown_receipt_rides_on_the_failed_step_report():
    server = Server()
    tracker = _tracker(server, ledger=_ledger())

    @tracker.track("charge", effect_type="irreversible")
    def charge(state):
        return tracker.effect("k1", "stripe.charge", lambda: (_ for _ in ()).throw(TimeoutError("t/o")), effect_type="irreversible")

    with pytest.raises(TimeoutError):
        charge({})
    sent = server.posted("/checkpoints/")[0]
    assert sent["status"] == "failed" and sent["side_effect_receipt"]["status"] == "unknown"
    assert sent["side_effect_receipt"]["business_key"] == "k1"


def test_succeeded_receipt_is_reported_when_a_later_line_in_the_step_fails():
    server = Server()
    tracker = _tracker(server, ledger=_ledger())

    @tracker.track("charge", effect_type="irreversible")
    def charge(state):
        tracker.effect("k1", "stripe.charge", lambda: {"id": "ch_1"}, effect_type="irreversible")
        raise ValueError("parsing crashed after the charge")

    with pytest.raises(ValueError):
        charge({})
    receipt = server.posted("/checkpoints/")[0]["side_effect_receipt"]
    assert receipt["status"] == "succeeded" and receipt["result"] == {"id": "ch_1"}


def test_effect_needs_a_ledger():
    with pytest.raises(ValueError):
        _tracker(Server()).effect("k", "t", lambda: 1, effect_type="none")


def _revalidate_answer(step_index=2):
    return {"run_status": "repairing", "resume": {"resume_from_step": step_index, "repaired_input": {"step_index": step_index}, "action": "revalidate"}}


def test_revalidate_runs_the_validator_reports_it_and_follows_the_servers_decision():
    server = Server()
    server.resume_answers.append({"run_status": "repairing", "resume": _revalidate_answer()["resume"]})
    server.revalidation_answer = {
        "run_status": "repairing",
        "resume": {"resume_from_step": 2, "repaired_input": {"resumate_confirmed": True, "confirmed_output": {"id": 7}}, "action": "resume"},
    }
    tracker = _tracker(server)
    tracker.register_validator(2, lambda: "effect_done")
    repaired = tracker.resume_from_last_failure()
    assert server.posted("/revalidation/") == [{"step_index": 2, "result": "effect_done"}]
    assert repaired == {"resumate_confirmed": True, "confirmed_output": {"id": 7}}
    assert tracker._step_index == 2
    assert len(server.posted("/consume/")) == 1


@pytest.mark.parametrize("setup", ["no_validator", "raises", "garbage"])
def test_revalidate_without_a_clear_answer_reports_unclear(setup):
    server = Server()
    server.resume_answers.append(_revalidate_answer())
    server.revalidation_answer = {"run_status": "escalated", "resume": {"resume_from_step": 2, "repaired_input": None, "action": None}}
    tracker = _tracker(server)
    if setup == "raises":
        tracker.register_validator(2, lambda: 1 / 0)
    if setup == "garbage":
        tracker.register_validator(2, lambda: "probably")
    assert tracker.resume_from_last_failure() is None
    assert server.posted("/revalidation/") == [{"step_index": 2, "result": "unclear"}]
    assert server.posted("/consume/") == []


def test_validator_found_by_step_name_and_result_recorded_in_the_ledger():
    server = Server()
    ledger = _ledger()
    tracker = _tracker(server, ledger=ledger)

    @tracker.track("charge", effect_type="irreversible", validator=lambda: ("effect_done", {"id": "found"}))
    def charge(state):
        return tracker.effect("k1", "stripe.charge", lambda: (_ for _ in ()).throw(TimeoutError()), effect_type="irreversible")

    with pytest.raises(TimeoutError):
        charge({})
    server.resume_answers.append(_revalidate_answer(step_index=0))
    server.revalidation_answer = {"run_status": "repairing", "resume": {"resume_from_step": 0, "repaired_input": {"resumate_confirmed": True}, "action": "resume"}}
    repaired = tracker.resume_from_last_failure()
    assert server.posted("/revalidation/") == [{"step_index": 0, "result": "effect_done"}]
    assert ledger.get("k1")["status"] == "succeeded"
    # the server sent no data (RESUMATE_STORE_STEP_DATA off): the SDK fills it from its own ledger
    assert repaired == {"resumate_confirmed": True, "confirmed_output": {"id": "found"}}


def test_confirmed_output_comes_from_the_own_ledger_in_a_fresh_process():
    ledger = _ledger()
    ledger.run("k9", "t", lambda: {"id": 42}, effect_type="irreversible")
    server = Server()
    server.resume_answers.append({"run_status": "repairing", "resume": {"resume_from_step": 3, "repaired_input": {"resumate_confirmed": True}, "action": "resume"}})
    tracker = _tracker(server, ledger=ledger)
    assert tracker.resume_from_last_failure(business_key="k9") == {"resumate_confirmed": True, "confirmed_output": {"id": 42}}


def test_held_run_gives_nothing_to_apply():
    server = Server()
    server.resume_answers.append({"run_status": "held", "resume": {"resume_from_step": 1, "repaired_input": None, "action": None}})
    assert _tracker(server).resume_from_last_failure() is None
    assert server.posted("/consume/") == []


class _State(TypedDict):
    trace: list
    charged: str


def _graph(tracker, log, fail):
    def a(state):
        log.append("a")
        return {"trace": ["a"]}

    def charge(state):
        log.append("charge")
        if fail["on"]:
            raise TimeoutError("lost")
        return {"charged": "yes"}

    def after(state):
        log.append("after")
        return {"trace": ["after"]}

    g = StateGraph(_State)
    g.add_node("a", tracker.track("a")(a))
    g.add_node("charge", tracker.track("charge")(charge))
    g.add_node("after", tracker.track("after")(after))
    g.set_entry_point("a")
    g.add_edge("a", "charge")
    g.add_edge("charge", "after")
    g.add_edge("after", END)
    return g.compile(checkpointer=MemorySaver())


def _fail_then(server, resume):
    server.resume_answers.append({"run_status": "repairing", "resume": resume})


def test_native_resume_skip_does_not_run_the_failed_node_and_continues_after_it():
    server = Server()
    tracker = _tracker(server)
    log, fail = [], {"on": True}
    app = _graph(tracker, log, fail)
    config = {"configurable": {"thread_id": "r1"}}
    with pytest.raises(TimeoutError):
        app.invoke({"trace": [], "charged": ""}, config=config)
    _fail_then(server, {"resume_from_step": 1, "repaired_input": {"resumate_skip": True}, "action": "resume"})
    with pytest.raises(ValueError):  # needs the node name; the instruction is not consumed yet
        tracker.resume_graph_native(app)
    assert server.posted("/consume/") == []
    _fail_then(server, {"resume_from_step": 1, "repaired_input": {"resumate_skip": True}, "action": "resume"})
    result = tracker.resume_graph_native(app, skip_as_node="charge")
    assert log.count("charge") == 1 and log[-1] == "after"
    assert result["charged"] == ""
    assert len(server.posted("/consume/")) == 1


def test_native_resume_confirmed_requires_state_updates_before_consuming():
    server = Server()
    tracker = _tracker(server)
    _fail_then(server, {"resume_from_step": 1, "repaired_input": {"resumate_confirmed": True, "confirmed_output": "x"}, "action": "resume"})
    with pytest.raises(ValueError):
        tracker.resume_graph_native(object())
    assert server.posted("/consume/") == []


def test_native_resume_on_a_held_run_does_nothing():
    server = Server()
    tracker = _tracker(server)
    server.resume_answers.append({"run_status": "escalated", "resume": {"resume_from_step": 1, "repaired_input": None, "action": None}})
    assert tracker.resume_graph_native(object()) is None


def test_exactly_one_charge_across_unknown_then_effect_done_then_native_resume():
    """End to end: timeout -> unknown -> server asks to revalidate -> validator says done -> graph resumes, tool not called again."""
    server = Server()
    ledger = _ledger()
    tracker = _tracker(server, ledger=ledger)
    provider_calls = {"n": 0}
    state_log = []

    def charge_node(state):
        def call(c):
            provider_calls["n"] += 1
            if provider_calls["n"] == 1:
                raise TimeoutError("response lost (the charge did land)")
            return {"id": "never reached"}

        out = tracker.effect("order-1", "stripe.charge", call, effect_type="irreversible")
        state_log.append(out)
        return {"charged": out["id"]}

    def only(state):
        return {}

    g = StateGraph(_State)
    g.add_node("charge", tracker.track("charge", effect_type="irreversible", validator=lambda: ("effect_done", {"id": "ch_77"}))(charge_node))
    g.set_entry_point("charge")
    g.add_edge("charge", END)
    app = g.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "r1"}}
    with pytest.raises(TimeoutError):
        app.invoke({"trace": [], "charged": ""}, config=config)

    server.resume_answers.append(_revalidate_answer(step_index=0))
    server.revalidation_answer = {"run_status": "repairing", "resume": {"resume_from_step": 0, "repaired_input": {"resumate_confirmed": True}, "action": "resume"}}
    final = tracker.resume_graph_native(app, state_updates={})
    assert provider_calls["n"] == 1, "the provider was called exactly once"
    assert final["charged"] == "ch_77"
