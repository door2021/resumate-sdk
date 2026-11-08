from __future__ import annotations

import contextvars
import functools
import logging
from typing import Any, Callable

from .client import ResumateClient
from .ledger import EFFECT_ABSENT, EFFECT_DONE, EFFECT_TYPES, UNCLEAR, VALIDATOR_ANSWERS, Ledger, _parse_answer

logger = logging.getLogger("resumate_sdk")

# The step currently running in this thread/task, so tracker.effect() can find its index.
_CURRENT_STEP: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("resumate_step", default=None)


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

    def __init__(self, client: ResumateClient, agent_name: str, run_id: str, ledger: Ledger | None = None):
        self.client = client
        self.agent_name = agent_name
        self.run_id = run_id
        self.ledger = ledger
        self._step_index = 0
        self._step_names: dict[int, str] = {}
        self._validators: dict[Any, Callable[[], Any]] = {}  # keyed by step name or step index
        self._effect_keys: dict[int, str] = {}  # step index -> business key used in that step
        self._waiting_for_person = False

    def register_validator(self, step: str | int, validator: Callable[[], Any]) -> None:
        """
        Register the check that answers "did this step's effect happen?" for a
        step name (or step index). It returns "effect_done", "effect_absent" or
        "unclear", or a pair (answer, result) to also hand back the result.
        Do NOT guess: if the real system cannot tell you, return "unclear".
        """
        self._validators[step] = validator

    def effect(
        self,
        business_key: str,
        tool: str,
        handler: Callable[..., Any],
        *,
        effect_type: str,
        validator: Callable[[], Any] | None = None,
        rejected_exceptions: tuple = (),
    ) -> Any:
        """
        Run a side-effecting call through this tracker's ledger, inside a
        tracked step. Returns the result (the stored one if it already
        happened). If the step later fails, the receipt is reported
        automatically; if the ledger raises (unknown, in progress) or the
        handler fails, the receipt rides on the exception.
        """
        if self.ledger is None:
            raise ValueError("LangGraphRunTracker needs ledger=... to use effect()")
        step = _CURRENT_STEP.get()
        if step is not None:
            self._effect_keys[step["index"]] = business_key
        result, receipt = self.ledger.run(
            business_key, tool, handler, effect_type=effect_type, validator=validator,
            rejected_exceptions=rejected_exceptions,
        )
        if step is not None:
            step["receipt"] = receipt
        return result

    def track(
        self,
        step_name: str,
        effect_type: str | None = None,
        validator: Callable[[], Any] | None = None,
    ) -> Callable:
        """
        If your node's code executed a tool call through
        resumate_sdk.ledger.Ledger and later raises (e.g. the call
        succeeded but something afterward crashed), the receipt is reported
        so the server knows not to blindly retry a side effect that already
        happened. Use `tracker.effect(...)` inside the step and this is
        automatic; otherwise attach the receipt yourself as
        `exc.side_effect_receipt` before raising.

        effect_type: "none", "repeat_safe", "reversible" or "irreversible".
        Declare it for every step: an undeclared step is treated as
        irreversible by the server, so its failures go to a person.

        validator: a no-argument function that checks the real system and
        returns "effect_done", "effect_absent" or "unclear" (or a pair with
        the result). It lets the server resolve an unclear step without a
        person. Registering it is what makes `validator_registered` true.
        """
        if effect_type is not None and effect_type not in EFFECT_TYPES:
            raise ValueError(f"effect_type must be one of {EFFECT_TYPES}, got {effect_type!r}")
        if validator is not None:
            self._validators[step_name] = validator
        gate_fields: dict[str, Any] = {}
        if effect_type is not None:
            gate_fields["effect_type"] = effect_type
        if effect_type is not None or validator is not None:
            gate_fields["validator_registered"] = (
                validator is not None or step_name in self._validators
            )

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapped(state: Any, *args, **kwargs):
                index = self._step_index
                self._step_index += 1
                self._step_names[index] = step_name
                ctx: dict[str, Any] = {"index": index, "receipt": None}
                token = _CURRENT_STEP.set(ctx)
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
                            side_effect_receipt=getattr(exc, "side_effect_receipt", None) or ctx["receipt"],
                            **gate_fields,
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
                finally:
                    _CURRENT_STEP.reset(token)
                output = result if isinstance(result, dict) else {"result": repr(result)}
                self.client.report_step(
                    agent_name=self.agent_name,
                    run_external_id=self.run_id,
                    step_index=index,
                    step_name=step_name,
                    status="succeeded",
                    output=output,
                    **gate_fields,
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

    def resume_graph_native(
        self,
        compiled_graph: Any,
        state_updates: dict[str, Any] | None = None,
        *,
        business_key: str | None = None,
        skip_as_node: str | None = None,
    ) -> Any:
        """
        Resumes a LangGraph graph via ITS OWN checkpointer, so already-
        succeeded nodes are not re-executed by LangGraph (verified against a
        real LangGraph: only the failed node runs again).

        REQUIRES: `compiled_graph` was compiled with a real checkpointer and
        the ORIGINAL invoke() used `config={"configurable": {"thread_id":
        self.run_id}}`.

        What the server may tell us, and what this does:
          - repair (normal): applies `state_updates` (or the server's
            repaired_input when its keys match your State) and resumes.
          - resumate_confirmed: the effect already happened. `state_updates`
            is REQUIRED (only you know your State schema); ValueError
            otherwise. The failed node runs again, and because it goes
            through the ledger it gets the stored result back without
            calling the tool a second time.
          - resumate_skip (a person chose "skip"): the failed node is NOT run.
            `skip_as_node` (the failed node's name) is REQUIRED; the graph is
            told that node finished and continues after it.
          - action "revalidate": the registered validator is run first and its
            answer reported; the server then decides.
          - hold / escalated: nothing to apply, returns None and the graph is
            left untouched.
        All argument checks happen BEFORE the instruction is consumed, so a
        ValueError does not lose the pending instruction.
        """
        repaired_input = self._resolve_resume(business_key)
        if self._waiting_for_person:
            return None
        had_instruction = repaired_input is not None
        if repaired_input is None:
            repaired_input = {}  # nothing to apply: native resume just continues

        skipping = bool(repaired_input.get("resumate_skip"))
        confirmed = bool(repaired_input.get("resumate_confirmed"))
        if skipping and skip_as_node is None:
            raise ValueError(
                "A person chose to skip the failed step (resumate_skip=True). Pass "
                "skip_as_node='<name of the failed node>' so the graph continues after it."
            )
        if confirmed and state_updates is None:
            raise ValueError(
                "The effect of this step already happened (resumate_confirmed=True), but no "
                "state_updates was given to say how repaired_input['confirmed_output'] maps onto "
                "your State. Pass state_updates, e.g. state_updates={'charge_result': "
                "repaired_input['confirmed_output']}."
            )
        if had_instruction:
            self.consume_resume()

        config = {"configurable": {"thread_id": self.run_id}}
        if skipping:
            compiled_graph.update_state(config, state_updates or {}, as_node=skip_as_node)
        else:
            if state_updates is None:
                state_updates = repaired_input
            if state_updates:
                compiled_graph.update_state(config, state_updates)
        return compiled_graph.invoke(None, config=config)

    def resume_from_last_failure(self, business_key: str | None = None) -> dict[str, Any] | None:
        """
        Call on a FRESH tracker when you are about to resume a failed run.
        Fetches the resume point, sets the step index so your next tracked
        call reports the right step_index, consumes the pending instruction
        and returns the repaired_input dict, or None when there is nothing
        to resume (no instruction, a hold, or a run waiting for a person).

        What can come back:
          {"timeout_seconds": 30, ...}   a repair: use it for the retry
          {"resumate_confirmed": True, "confirmed_output": ...}
                                          the effect already happened: do NOT
                                          call the tool again. confirmed_output
                                          comes from the server, or - when the
                                          server is not storing step data -
                                          from this tracker's own ledger
                                          (pass business_key= if this is a
                                          fresh process that did not run the step)
          {"resumate_skip": True}        a person chose to skip this step

        If the server asked for a revalidation first, the registered
        validator runs, its answer is reported, and what you get back is the
        server's decision after that.

        This only says WHERE to resume and WITH WHAT; it does not stop
        LangGraph from re-running earlier nodes if you invoke the whole graph
        again. Use resume_graph_native() for that.
        """
        repaired_input = self._resolve_resume(business_key)
        if repaired_input is not None:
            self.consume_resume()
        return repaired_input

    # --- resume internals ------------------------------------------------------
    def _resolve_resume(self, business_key: str | None) -> dict[str, Any] | None:
        info = self.check_resume()
        resume = info.get("resume", {}) or {}
        self._waiting_for_person = False
        if resume.get("action") == "revalidate":
            resume = self._revalidate(resume)
        self._step_index = resume.get("resume_from_step", 0)
        if resume.get("action") == "revalidate":  # the server asked again: do not loop, wait for a person
            logger.warning("resumate_sdk: run %s still needs revalidation; leaving it for a person", self.run_id)
            return None
        repaired_input = resume.get("repaired_input")
        if repaired_input is None:
            # A held or escalated run has no instruction until a person decides: do nothing.
            self._waiting_for_person = (info.get("run_status") or resume.get("run_status")) in ("held", "escalated")
            return None
        if repaired_input.get("resumate_confirmed") and repaired_input.get("confirmed_output") is None:
            output = self._confirmed_output_from_ledger(business_key, self._step_index)
            if output is not _MISSING:
                repaired_input = {**repaired_input, "confirmed_output": output}
        return repaired_input

    def _revalidate(self, resume: dict[str, Any]) -> dict[str, Any]:
        """Run the validator for the step the server named, report the answer, return the new resume point."""
        step_index = (resume.get("repaired_input") or {}).get("step_index")
        if not isinstance(step_index, int):
            return resume
        answer, result = self._run_validator(step_index)
        key = self._effect_keys.get(step_index)
        if self.ledger is not None and key is not None and answer in (EFFECT_DONE, EFFECT_ABSENT):
            try:
                self.ledger.resolve(key, answer, result=result)
            except ValueError:
                pass  # not unknown/processing (already decided): nothing to record locally
        response = self.client.report_revalidation(self.run_id, step_index, answer)
        return response.get("resume", {}) or {"resume_from_step": self._step_index}

    def _run_validator(self, step_index: int) -> tuple[str, Any]:
        validator = self._validators.get(step_index) or self._validators.get(self._step_names.get(step_index, ""))
        if validator is None:
            logger.warning("resumate_sdk: no validator for step %s of run %s; answering unclear", step_index, self.run_id)
            return UNCLEAR, None
        try:
            answer, result = _parse_answer(validator())
        except Exception:
            logger.exception("resumate_sdk: validator for step %s raised; answering unclear", step_index)
            return UNCLEAR, None
        return (answer if answer in VALIDATOR_ANSWERS else UNCLEAR), result

    def _confirmed_output_from_ledger(self, business_key: str | None, step_index: int) -> Any:
        if self.ledger is None:
            return _MISSING
        key = business_key or self._effect_keys.get(step_index)
        if key is None:
            return _MISSING
        rec = self.ledger.get(key)
        if rec is None or rec["status"] != "succeeded":
            return _MISSING
        return rec["result"]


_MISSING = object()
