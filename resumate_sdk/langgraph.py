from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from .client import ResumateClient

logger = logging.getLogger("resumate_sdk")


class LangGraphRunTracker:
    """
    One instance per agent run. Wrap each LangGraph node function with
    `.track("step name")` — on success it checkpoints the output, on
    failure it reports the error and re-raises (so LangGraph's own
    execution still stops the run the way it normally would; Resumate's
    job is to record what happened and let you resume later, not to
    swallow the exception).

    Usage:
        tracker = LangGraphRunTracker(client, agent_name="research_agent", run_id=thread_id)

        @tracker.track("plan_query")
        def plan_query(state): ...

        @tracker.track("web_search")
        def web_search(state): ...

        graph = StateGraph(...)
        graph.add_node("plan_query", plan_query)
        graph.add_node("web_search", web_search)
        ...
        graph.compile().invoke(initial_state)
        tracker.mark_run_succeeded()  # call after invoke() returns without raising
    """

    def __init__(self, client: ResumateClient, agent_name: str, run_id: str):
        self.client = client
        self.agent_name = agent_name
        self.run_id = run_id
        self._step_index = 0

    def track(self, step_name: str) -> Callable:
        """
        If your node's code executed a tool call through
        resumate_sdk.ledger.run_idempotent() and later raises (e.g. the
        call succeeded but something afterward crashed), attach the
        resulting receipt to the exception as `.side_effect_receipt`
        before raising it - this decorator picks it up (via getattr, no
        specific exception class required) and reports it, so the server
        knows not to blindly retry a side effect that already happened:

            try:
                result, receipt = run_idempotent(ledger, self.run_id, "stripe.charge", args, do_charge)
            except SomeParsingError as exc:
                exc.side_effect_receipt = receipt  # only if you have one to attach
                raise
        """

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapped(state: Any, *args, **kwargs):
                index = self._step_index
                self._step_index += 1
                try:
                    result = fn(state, *args, **kwargs)
                except Exception as exc:
                    # The node's own exception must ALWAYS be what propagates
                    # here, even if reporting it also fails (e.g. the client
                    # is configured with fail_open=False). Reporting infra
                    # failing must never mask or replace the agent's real
                    # error - so this reporting call is isolated in its own
                    # try/except regardless of the client's fail_open setting.
                    try:
                        self.client.report_step(
                            agent_name=self.agent_name,
                            run_external_id=self.run_id,
                            step_index=index,
                            step_name=step_name,
                            status="failed",
                            error={"type": type(exc).__name__, "message": str(exc)},
                            side_effect_receipt=getattr(exc, "side_effect_receipt", None),
                        )
                    except Exception:
                        logger.exception(
                            "resumate_sdk: failed to report step %d (%s) failure for run %s "
                            "- re-raising the original error regardless",
                            index,
                            step_name,
                            self.run_id,
                        )
                    raise
                output = result if isinstance(result, dict) else {"result": repr(result)}
                self.client.report_step(
                    agent_name=self.agent_name,
                    run_external_id=self.run_id,
                    step_index=index,
                    step_name=step_name,
                    status="succeeded",
                    output=output,
                )
                return result

            return wrapped

        return decorator

    def mark_run_succeeded(self) -> None:
        """Call once after the graph finishes without raising."""
        self.client.report_step(
            agent_name=self.agent_name,
            run_external_id=self.run_id,
            step_index=self._step_index,
            step_name="__run_complete__",
            status="succeeded",
            run_status="succeeded",
        )
        self._step_index += 1

    def check_resume(self) -> dict[str, Any]:
        """Poll after a step fails: tells you the resume point and any repaired input."""
        return self.client.get_resume(self.run_id)

    def consume_resume(self) -> None:
        """Call after you've actually applied a repaired input and resumed locally."""
        self.client.consume_resume(self.run_id)

    def resume_graph_native(self, compiled_graph: Any, state_updates: dict[str, Any] | None = None) -> Any:
        """
        Resumes a LangGraph graph via ITS OWN checkpointer, so already-
        succeeded nodes are genuinely not re-executed - not just not
        double-recorded on Resumate's side, but never actually re-run by
        LangGraph at all. Verified against a real installed LangGraph
        (see doc.md 2.18): a node BEFORE the failure point executes
        exactly once total across a fail-then-resume cycle; only the
        failed node itself re-executes.

        REQUIRES: `compiled_graph` was `graph.compile(checkpointer=...)`'d
        with a real LangGraph checkpointer (e.g.
        `langgraph.checkpoint.memory.MemorySaver` for a single process,
        a durable store for crash recovery), and the ORIGINAL failed
        `invoke()` call used `config={"configurable": {"thread_id":
        self.run_id}}` - this method reuses that same thread_id.

        Internally calls resume_from_last_failure() (fetches the resume
        point, positions this tracker's step index correctly, consumes
        the pending instruction), then:
          - if `state_updates` is given, applies exactly that via
            `compiled_graph.update_state()` before resuming - use this
            when you know how to translate the repair into your own
            State schema.
          - otherwise, if the server's repaired_input is the special
            ledger-confirmed-receipt marker (`resumate_confirmed: True`
            - see resumate_sdk.ledger), `state_updates` is REQUIRED:
            raises ValueError rather than guessing how to map
            `confirmed_output` onto your State fields, since only you
            know that schema.
          - otherwise, applies the server's repaired_input as-is (works
            when its keys already match your State's field names - the
            common case for e.g. {"timeout_seconds": 30}).

        Then calls `compiled_graph.invoke(None, config=...)` -
        LangGraph's own engine resumes from its last checkpoint. Returns
        the graph's final state.
        """
        repaired_input = self.resume_from_last_failure()

        if repaired_input and repaired_input.get("resumate_confirmed") and state_updates is None:
            raise ValueError(
                "The server confirmed this step's side effect already completed via an "
                "agent-ledger receipt (resumate_confirmed=True), but no state_updates was "
                "given to tell native resume how to apply "
                "repaired_input['confirmed_output'] to your graph's State schema. Pass "
                "state_updates explicitly, e.g. "
                "state_updates={'my_state_field': repaired_input['confirmed_output']}."
            )
        if state_updates is None and repaired_input:
            state_updates = repaired_input

        config = {"configurable": {"thread_id": self.run_id}}
        if state_updates:
            compiled_graph.update_state(config, state_updates)

        return compiled_graph.invoke(None, config=config)

    def resume_from_last_failure(self) -> dict[str, Any] | None:
        """
        Call this on a FRESH tracker when you're about to resume a run
        that previously failed, instead of manually poking `_step_index`.
        Fetches the resume point from the server, sets this tracker's
        step index so your next `.track()`-wrapped call reports the
        correct step_index (not step 0), consumes the pending repair
        instruction if there is one, and returns the repaired_input dict
        for you to apply - or None if there's nothing to resume.

        Usage:
            tracker = LangGraphRunTracker(client, agent_name=..., run_id=run_id)
            repaired_input = tracker.resume_from_last_failure()
            if repaired_input and repaired_input.get("resumate_confirmed"):
                # The server confirmed (via an agent-ledger receipt) that
                # this step's side effect already happened - do NOT
                # re-invoke the tool. Use the confirmed result directly.
                output = repaired_input["confirmed_output"]
            else:
                timeout = (repaired_input or {}).get("timeout_seconds", DEFAULT_TIMEOUT)
                # ...now call ONLY the node(s) that still need to run, wrapped
                # with .track() as usual, starting from the failed step onward.

        WHAT THIS DOES NOT DO, READ BEFORE RELYING ON IT: this only tells
        you WHERE to resume from and WHAT repaired input to use - it does
        NOT make LangGraph itself skip re-running already-succeeded node
        functions. If you call `graph.invoke(...)` on the whole compiled
        graph again from scratch, LangGraph will re-execute every earlier
        node (Resumate correctly avoids double-RECORDING those steps, but
        your own node code still runs again - you still pay for those
        LLM/tool calls a second time). To actually skip re-running
        completed nodes, you must invoke only the remaining node(s)
        yourself starting from `resume_from_step`, OR integrate with
        LangGraph's own checkpointer (MemorySaver/thread_id/interrupts)
        so LangGraph's execution engine itself resumes mid-graph - that
        deeper integration is not built by this SDK today.
        """
        info = self.check_resume()
        resume = info.get("resume", {})
        self._step_index = resume.get("resume_from_step", 0)

        repaired_input = resume.get("repaired_input")
        if repaired_input is not None:
            self.consume_resume()
        return repaired_input
