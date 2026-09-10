"""
Tests for LangGraphRunTracker.resume_graph_native() against a REAL
langgraph.StateGraph + MemorySaver, not a mock of LangGraph's API -
the whole point of this feature is a specific claim about LangGraph's
own execution engine, which only a real graph can actually prove.
"""

import httpx
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from resumate_sdk import LangGraphRunTracker, ResumateClient


class _State(TypedDict):
    query: str
    results: list
    page_content: str


def _client(handler, **kwargs):
    return ResumateClient(api_key="k", base_url="http://test", transport=httpx.MockTransport(handler), **kwargs)


def _build_graph(tracker, call_log, fail_fetch_flag):
    def plan_query(state):
        call_log.append("plan_query")
        return {"query": "q"}

    def web_search(state):
        call_log.append("web_search")
        return {"results": ["a"]}

    def fetch_page(state):
        call_log.append("fetch_page")
        if fail_fetch_flag["should_fail"]:
            raise TimeoutError("upstream took too long")
        return {"page_content": "ok"}

    graph = StateGraph(_State)
    graph.add_node("plan_query", tracker.track("plan_query")(plan_query))
    graph.add_node("web_search", tracker.track("web_search")(web_search))
    graph.add_node("fetch_page", tracker.track("fetch_page")(fetch_page))
    graph.set_entry_point("plan_query")
    graph.add_edge("plan_query", "web_search")
    graph.add_edge("web_search", "fetch_page")
    graph.add_edge("fetch_page", END)
    return graph.compile(checkpointer=MemorySaver())


def test_native_resume_does_not_re_execute_already_succeeded_nodes():
    """The actual claim this feature exists to prove."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "step_recorded": True,
                "run_status": "repairing",
                "resume": {"resume_from_step": 2, "repaired_input": None},
            },
        )

    client = _client(handler)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="native_1")
    call_log = []
    fail_flag = {"should_fail": True}
    app = _build_graph(tracker, call_log, fail_flag)
    config = {"configurable": {"thread_id": "native_1"}}

    try:
        app.invoke({"query": "", "results": [], "page_content": ""}, config=config)
    except TimeoutError:
        pass

    assert call_log == ["plan_query", "web_search", "fetch_page"]

    fail_flag["should_fail"] = False
    tracker.resume_graph_native(app)

    assert call_log.count("plan_query") == 1, "must NOT re-execute - this is the whole point"
    assert call_log.count("web_search") == 1, "must NOT re-execute - this is the whole point"
    assert call_log.count("fetch_page") == 2, "the failed node SHOULD re-execute"


def test_native_resume_applies_repaired_input_via_state_before_retrying():
    class TimeoutState(TypedDict):
        timeout_seconds: int
        result: str

    attempts = []

    def flaky(state):
        timeout = state.get("timeout_seconds", 5)
        attempts.append(timeout)
        if timeout < 30:
            raise TimeoutError("too short")
        return {"result": "ok"}

    def handler(request):
        return httpx.Response(
            200,
            json={
                "step_recorded": True,
                "run_status": "repairing",
                "resume": {"resume_from_step": 0, "repaired_input": {"timeout_seconds": 30}},
            },
        )

    client = _client(handler)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="native_2")
    graph = StateGraph(TimeoutState)
    graph.add_node("flaky", tracker.track("flaky")(flaky))
    graph.set_entry_point("flaky")
    graph.add_edge("flaky", END)
    app = graph.compile(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "native_2"}}

    try:
        app.invoke({"timeout_seconds": 5, "result": ""}, config=config)
    except TimeoutError:
        pass

    result = tracker.resume_graph_native(app)
    assert attempts == [5, 30], "the injected repaired_input must be what the retry actually used"
    assert result["result"] == "ok"


def test_native_resume_raises_without_state_updates_for_confirmed_receipt_case():
    """Must not silently guess how to map confirmed_output onto an arbitrary State schema."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "step_recorded": True,
                "run_status": "repairing",
                "resume": {
                    "resume_from_step": 0,
                    "repaired_input": {"resumate_confirmed": True, "confirmed_output": {"id": "ch_1"}},
                },
            },
        )

    client = _client(handler)
    tracker = LangGraphRunTracker(client, agent_name="a", run_id="native_3")

    class DummyGraph:
        def update_state(self, *a, **k):
            raise AssertionError("must not reach update_state without state_updates being resolved")

        def invoke(self, *a, **k):
            raise AssertionError("must not reach invoke without state_updates being resolved")

    try:
        tracker.resume_graph_native(DummyGraph())
        assert False, "expected ValueError"
    except ValueError as e:
        assert "state_updates" in str(e)
