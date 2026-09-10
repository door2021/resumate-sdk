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

## Resuming after a failure
```python
tracker = LangGraphRunTracker(client, agent_name="research_agent", run_id=thread_id)
repaired_input = tracker.resume_from_last_failure()
timeout = (repaired_input or {}).get("timeout_seconds", DEFAULT_TIMEOUT)

# Invoke ONLY the node(s) that still need to run, starting from the
# failed step - NOT the whole graph from scratch. See the important
# limitation below.
```

**Important limitation:** `resume_from_last_failure()` tells you *where*
to resume from and *what* repaired input to use - it does NOT make
LangGraph itself skip re-running already-succeeded nodes. Calling
`graph.invoke(...)` on the whole compiled graph again will make LangGraph
re-execute every earlier node (Resumate won't double-record those steps,
but your own node code - and any LLM/tool calls in it - runs again). To
actually avoid re-paying for completed work, invoke only the remaining
node(s) yourself, or integrate with LangGraph's own checkpointer
(`MemorySaver`/`thread_id`/interrupts) so LangGraph's execution engine
resumes mid-graph on its own - that deeper integration isn't built by
this SDK yet.

## Preventing duplicate side effects (`resumate_sdk.ledger`)

Install with `pip install "resumate-sdk[ledger]"` (pulls in `agent-ledger`,
a real third-party idempotency library - not something Resumate built).
Wrap any side-effecting tool call so a retry can never fire it twice:

```python
from resumate_sdk.ledger import default_ledger, run_idempotent

ledger = default_ledger()  # in-memory, single-worker; see run_idempotent's
                             # docstring for durable/multi-worker options

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
`resume_from_last_failure()` replaces manually poking `tracker._step_index`
(verified against a real live server + real LangGraph run) — but read its
docstring/the limitation above before relying on it: it does not make
LangGraph skip re-executing already-succeeded nodes on its own.
`resumate_sdk.ledger` (optional `[ledger]` extra) was verified against a
real installed copy of `agent-ledger`, including a full end-to-end run
through a live Django server confirming a simulated "Stripe charge" fires
exactly once across a failure-and-resume cycle - see the main repo's
doc.md section 2.17.
Test suite: 13 tests covering retry recovery, fail-open, the
original-exception-always-wins guarantee, fail-fast on non-retryable
errors, `resume_from_last_failure()`'s two paths, `.side_effect_receipt`
propagation through `track()`, and the ledger module's real dedup
behavior — run with:

    pip install -e ".[dev]"
    python -m pytest tests/ -v

Also runs on every push via the main repo's `.github/workflows/tests.yml`.
