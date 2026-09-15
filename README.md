# resumate-sdk

Client-side package for LangGraph agents. Install this into your own
agent process - it talks to a Resumate server over HTTP so failed steps
get checkpointed, repaired, and resumed without re-running work you've
already paid for.

This is intentionally a separate installable package from the Django
server (see `../README.md`), not code living inside it: it ships
independently, gets versioned independently, and is the only piece of
Resumate that runs inside a customer's own process.

## Install
    pip install -e .   # from this directory, during local dev
    # pip install resumate-sdk  # once published

## Usage
```python
from resumate_sdk import ResumateClient, LangGraphRunTracker

client = ResumateClient(api_key="rsm_live_...", base_url="https://your-resumate-server")
tracker = LangGraphRunTracker(client, agent_name="research_agent", run_id=thread_id)

@tracker.track("web_search")
def web_search(state):
    ...

# wrap every node you want checkpointed, build/compile the graph as usual
graph.invoke(initial_state)
tracker.mark_run_succeeded()
```

If a node raises, the exception is reported to the server and re-raised
(Resumate records failures, it doesn't swallow them).

## Resuming after a failure — two ways

**Recommended: native resume.** Compile your graph with a real LangGraph
checkpointer and use `resume_graph_native()` — LangGraph's own execution
engine resumes from its last checkpoint, so already-succeeded nodes are
genuinely not re-executed (verified against real LangGraph — see the main
repo's doc.md section 2.18):

```python
from langgraph.checkpoint.memory import MemorySaver

tracker = LangGraphRunTracker(client, agent_name="research_agent", run_id=thread_id)
config = {"configurable": {"thread_id": thread_id}}
app = graph.compile(checkpointer=MemorySaver())  # or a durable store for crash recovery

try:
    app.invoke(initial_state, config=config)
except Exception:
    result = tracker.resume_graph_native(app)  # applies the repair via graph.update_state(),
                                                 # then invoke(None, config) - LangGraph resumes natively
```

If the repair is the ledger-confirmed-receipt case (see below), you must
tell `resume_graph_native()` how to map `confirmed_output` onto your own
State schema via `state_updates=` — it raises `ValueError` rather than
guessing:
```python
tracker.resume_graph_native(app, state_updates={"charge_result": repaired_input["confirmed_output"]})
```

**Manual/lower-level: `resume_from_last_failure()`.** Still available for
when you don't want to compile with a checkpointer, or need to invoke
only specific remaining node(s) yourself:
```python
tracker = LangGraphRunTracker(client, agent_name="research_agent", run_id=thread_id)
repaired_input = tracker.resume_from_last_failure()
timeout = (repaired_input or {}).get("timeout_seconds", DEFAULT_TIMEOUT)
# Invoke ONLY the node(s) that still need to run, starting from the
# failed step - NOT the whole graph from scratch, unless you're using
# resume_graph_native() above, which handles this correctly for you.
```

## Preventing duplicate side effects (`resumate_sdk.ledger`)

Install with `pip install "resumate-sdk[ledger]"` (pulls in `agent-ledger`,
a real third-party idempotency library - not something Resumate built).
Wrap any side-effecting tool call so a retry can never fire it twice:

**Use a durable store for anything you actually care about not double-firing.**
`default_ledger()` (in-memory) is fine for local testing, but it does NOT
survive a process crash - tested directly: a crash between the side effect
succeeding and the ledger recording it, followed by a process restart with
a fresh `default_ledger()`, re-fires the side effect. For anything real
(payments, emails, database writes), point `EffectLedger` at your own
durable store instead:

```python
from agent_ledger import EffectLedger, EffectLedgerOptions
from agent_ledger.stores.postgres import PostgresStore
from resumate_sdk.ledger import run_idempotent
# pool = your own AsyncConnectionPool (psycopg_pool), any Postgres you run -
# does not need to be the same database your agent otherwise uses.

ledger = EffectLedger(EffectLedgerOptions(store=PostgresStore(pool)))

@tracker.track("Tool: stripe_charge")
def charge_node(state):
    result, receipt = run_idempotent(
        ledger, tracker.run_id, "stripe.charge", {"amount": 50, "customer": "cus_1"},
        lambda: stripe.Charge.create(amount=50, customer="cus_1"),
    )
    if something_after_the_charge_fails:
        exc = SomeError("parsing crashed after the charge went through")
        exc.side_effect_receipt = receipt  # tells the server not to blindly retry
        raise exc
    return result
```

With a durable store, a crash right after the side effect succeeds (before
the ledger commits that fact) is safe - the effect is claimed *before* the
handler runs, so a crash mid-flight leaves it stuck rather than duplicated;
a retry with the same key waits/times out rather than re-firing.

**A real limitation, stated plainly rather than glossed over:** a network
timeout (request sent, response never came back - so you genuinely don't
know if it landed) is currently classified the same as a confirmed
rejection - there's no separate "unknown, go check manually" state yet.
Tested directly: a timed-out call gets marked `failed` and permanently
blocks retry on that idempotency key, whether or not the side effect
actually happened. This is safe in the narrow sense (no double-fire), but
it means "failed" in your dashboard doesn't always mean "definitely didn't
happen" - for a timed-out side effect, verify with the provider directly
before assuming it didn't land.

If the step then fails, `track()` picks up `.side_effect_receipt` off the
exception automatically and reports it. If the receipt shows the charge
actually succeeded, the Resumate server resolves the step via the
confirmed result instead of proposing a retry that could double-charge -
check for this on resume:

```python
repaired_input = tracker.resume_from_last_failure()
if repaired_input and repaired_input.get("resumate_confirmed"):
    output = repaired_input["confirmed_output"]  # do NOT re-run the tool call
```

**Why this lives here, not on the Resumate server:** an idempotency ledger
has to wrap actual tool execution to prevent a duplicate call - only your
own process ever executes that call, so this is the only place real
protection can live. The server can only ever consult a receipt after the
fact.

## Status
Real retry/backoff/fail-open handling — see `client.py` and `exceptions.py`.
`resume_graph_native()` closes the real-LangGraph-resume gap — verified
against an installed LangGraph directly: nodes before the failure point
execute exactly once total across a fail-then-resume cycle, confirmed
end-to-end against a live server with an auto-generated repair. Use it in
preference to `resume_from_last_failure()`'s manual node-by-node approach
unless you have a specific reason not to (e.g. not compiling with a
checkpointer). `resumate_sdk.ledger` (optional `[ledger]` extra) was
verified against a real installed copy of `agent-ledger`, including a full
end-to-end run through a live Django server confirming a simulated
"Stripe charge" fires exactly once across a failure-and-resume cycle —
see the main repo's doc.md sections 2.17-2.18.
Test suite: 16 tests covering retry recovery, fail-open, the
original-exception-always-wins guarantee, fail-fast on non-retryable
errors, `resume_from_last_failure()`'s two paths, `.side_effect_receipt`
propagation through `track()`, the ledger module's real dedup behavior,
and native resume's no-re-execution guarantee against a real LangGraph
graph — run with:

    pip install -e ".[dev]"
    python -m pytest tests/ -v

Also runs on every push via the main repo's `.github/workflows/tests.yml`.
