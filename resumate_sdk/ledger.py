"""
Optional integration with agent-ledger (https://pypi.org/project/agent-ledger/)
for side-effect-safe tool execution.

WHY THIS LIVES HERE, NOT ON THE RESUMATE SERVER: an idempotency ledger has
to wrap the ACTUAL execution of a tool call to prevent a duplicate side
effect - and only the customer's own process ever executes that call. The
Resumate server can only ever be told about a call after the fact. So the
real protection is here; the server-side attempt_repair() only CONSULTS
the receipt this module produces (see report_step's side_effect_receipt
param and doc.md section 2.17) - it cannot create that protection itself.

This is a genuinely optional extra - importing this module without
agent-ledger installed raises a clear, actionable ImportError rather than
a confusing one from deep inside agent_ledger.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

try:
    from agent_ledger import EffectLedger, EffectLedgerOptions, MemoryStore, ToolCall
except ImportError as exc:
    raise ImportError(
        "agent-ledger is required for idempotent tool execution. "
        "Install with: pip install resumate-sdk[ledger]"
    ) from exc


def default_ledger() -> EffectLedger:
    """
    A process-local, in-memory ledger - fine for a single agent worker,
    not durable across a process crash or shared across multiple workers.
    For crash-recovery/multi-worker guarantees, construct your own
    EffectLedger with a durable store (see agent-ledger's own docs) and
    pass it to run_idempotent's `ledger=` argument instead.
    """
    return EffectLedger(EffectLedgerOptions(store=MemoryStore()))


def run_idempotent(
    ledger: EffectLedger,
    workflow_id: str,
    tool: str,
    args: dict[str, Any],
    handler: Callable[[], Any],
) -> tuple[Any, dict[str, Any]]:
    """
    Executes `handler` at-most-once for this exact (workflow_id, tool,
    args) combination - calling this again with the same three values
    returns the cached result instead of re-executing, verified directly
    against a real agent-ledger call before this was written (see doc.md
    2.17).

    Returns (result, receipt). `receipt` is a small JSON-safe dict meant
    to be passed as ResumateClient.report_step's side_effect_receipt -
    this is what lets the Resumate server know a side effect already
    definitively completed, even if the step later reports as failed
    (e.g. the call succeeded but something after it crashed).

    LIMITATION, STATED PLAINLY: this is a SYNC convenience wrapper
    (uses asyncio.run() internally), matching how LangGraphRunTracker.track
    wraps plain sync node functions today. Calling this from an already-
    running event loop (an async LangGraph node) will raise - in that
    case, call agent_ledger's EffectLedger.run()/find_by_idem_key()
    directly instead, which are natively async.
    """
    call = ToolCall(workflow_id=workflow_id, tool=tool, args=args)

    async def _handler(effect):
        result = handler()
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def _run_and_fetch():
        result = await ledger.run(call, _handler)
        effect = await ledger.find_by_idem_key(ledger.idem_key(call))
        return result, effect

    result, effect = asyncio.run(_run_and_fetch())
    receipt = {
        "tool": tool,
        "idem_key": effect.idem_key,
        "status": effect.status.value,
        "result": effect.result,
        "dedup_count": effect.dedup_count,
    }
    return result, receipt
