"""
Resumate's own effect ledger.

Why it exists: a retry must never repeat a side effect (a charge, an email,
a database write) that already happened, and when we do NOT know whether it
happened, the only honest answer is "unknown", not "failed".

How it works (claim first):

1. Before the tool runs, the ledger records the call as `processing`, keyed
   by a BUSINESS key (what the call is about, e.g. the order id and the line
   item), never by a run id, an execution id or anything a model generated.
2. The tool runs. On success the entry becomes `succeeded` and keeps the
   result. A retry with the same business key gets that stored result back
   and the tool is not called.
3. If the tool raises a clear rejection (EffectRejected, or an exception type
   you listed as a rejection) the entry becomes `failed` and a retry may run
   again. ANY OTHER exception, including timeouts and lost connections,
   becomes `unknown`: we cannot tell whether the effect landed.
4. An `unknown` entry is never retried by the ledger on its own. Either a
   validator you registered asks the real system ("did it happen?"), or a
   person calls `ledger.resolve(...)`, or the call is reported to the
   Resumate server, which escalates it.
5. A `processing` entry left behind by a process that died is turned into
   `unknown` at startup (or when it is older than `stale_after`). The ledger
   never takes over such an entry and re-runs it.

The same provider key (`ledger.provider_key(business_key)`) is returned on
every retry, so you can pass it as the provider's Idempotency-Key header.

This is not an exactly-once guarantee. It narrows the window in which a
duplicate can happen and makes the remaining cases visible (unknown).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .stores import MemoryStore

logger = logging.getLogger("resumate_sdk")

EFFECT_TYPES = ("none", "repeat_safe", "reversible", "irreversible")
_RERUN_SAFE = ("none", "repeat_safe")

EFFECT_DONE = "effect_done"
EFFECT_ABSENT = "effect_absent"
UNCLEAR = "unclear"
VALIDATOR_ANSWERS = (EFFECT_DONE, EFFECT_ABSENT, UNCLEAR)

MAX_ERROR_CHARS = 300


# --- errors -------------------------------------------------------------------
class EffectRejected(Exception):
    """Raise this from a handler when the other system CLEARLY refused the call
    (it did not happen). The entry becomes `failed` and a retry may run again."""


class LedgerError(Exception):
    """Base class. Carries the receipt so the tracker can report it."""

    def __init__(self, message: str, receipt: dict[str, Any]):
        super().__init__(message)
        self.receipt = receipt
        self.side_effect_receipt = receipt


class EffectInProgress(LedgerError):
    """Another call with this business key is running right now."""


class EffectUnknown(LedgerError):
    """We do not know whether this effect happened. Do not re-run it blindly."""


# --- keys and redaction --------------------------------------------------------
def make_key(tool: str, business_object: Any, item_index: int | None = None) -> str:
    """
    Build a business key from WHAT the call is about: the tool, the business
    object (order id, invoice number, recipient plus template...) and, for
    batches, the item index. Never put a run id, execution id, timestamp or
    model-generated text in `business_object`.
    """
    canonical = json.dumps(business_object, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    key = f"{tool}:{digest}"
    if item_index is not None:
        key += f"#{int(item_index)}"
    return key


_SENSITIVE_KEY = re.compile(
    r"(pass(word)?|secret|token|authori[sz]ation|api[_-]?key|cookie|card|cvv|cvc|ssn|private[_-]?key)", re.I
)
_TEXT_PATTERNS = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [redacted]"),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b\s*[:=]\s*\S+"),
        r"\1=[redacted]",
    ),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "[token]"),
    (re.compile(r"\b\d{13,19}\b"), "[number]"),
)


def redact_text(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    for pattern, repl in _TEXT_PATTERNS:
        text = pattern.sub(repl, text)
    return text[:limit]


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Copy of `value` with the values of sensitive-looking keys replaced."""
    if _depth > 12:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if _SENSITIVE_KEY.search(str(k)) else redact_value(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(v, _depth + 1) for v in value]
    return value


# --- small value objects ---------------------------------------------------------
@dataclass(frozen=True)
class Call:
    """Passed to a handler that declares one parameter."""

    business_key: str
    provider_key: str
    attempt: int
    tool: str


@dataclass
class SelfCheckReport:
    ok: bool
    store: str
    durable: bool
    single_worker: bool
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    marked_unknown: list[str] = field(default_factory=list)
    unknown_total: int = 0

    def to_text(self) -> str:
        lines = [f"ledger self-check: {'OK' if self.ok else 'PROBLEMS'} (store={self.store}, durable={self.durable})"]
        lines += [f"  problem: {p}" for p in self.problems]
        lines += [f"  warning: {w}" for w in self.warnings]
        lines.append(f"  stuck entries turned into unknown at startup: {len(self.marked_unknown)}")
        lines.append(f"  unknown entries waiting for a decision: {self.unknown_total}")
        return "\n".join(lines)


@dataclass
class ReconciliationReport:
    generated_at: float
    entries: list[dict[str, Any]]

    @property
    def needs_attention(self) -> bool:
        return bool(self.entries)

    def to_text(self) -> str:
        if not self.entries:
            return "reconciliation: nothing to resolve"
        lines = [f"reconciliation: {len(self.entries)} effect(s) need a decision (not known to have happened or not)"]
        for e in self.entries:
            lines.append(
                f"  - {e['business_key']} tool={e['tool']} effect={e['effect_type']} status={e['status']} "
                f"attempts={e['attempts']} age={int(e['age_seconds'])}s"
                + (f" error={e['error']}" if e.get("error") else "")
            )
        lines.append("  resolve each with ledger.resolve(key, 'effect_done' | 'effect_absent', result=...)")
        return "\n".join(lines)


# --- the ledger ------------------------------------------------------------------
class Ledger:
    """
    One Ledger per process. `store` decides durability:
    MemoryStore (tests), SQLiteStore (one worker), PostgresStore (many).

    stale_after: seconds after which a `processing` entry owned by ANOTHER
    process is treated as stuck and turned into `unknown`. For a
    single-worker store, every entry left by an earlier process is stuck at
    startup regardless of age.
    """

    def __init__(
        self,
        store: Any | None = None,
        *,
        stale_after: float = 600.0,
        rejected_exceptions: tuple[type[BaseException], ...] = (),
        receipt_includes_result: bool = True,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store if store is not None else MemoryStore()
        self.stale_after = stale_after
        self.rejected_exceptions = (EffectRejected, *rejected_exceptions)
        self.receipt_includes_result = receipt_includes_result
        self._clock = clock
        self.owner = uuid.uuid4().hex

    # -- keys ---
    @staticmethod
    def provider_key(business_key: str) -> str:
        """The same value on every retry. Pass it to the provider as its idempotency key."""
        return "rsm_" + hashlib.sha256(business_key.encode()).hexdigest()[:40]

    # -- reading ---
    def get(self, business_key: str) -> dict[str, Any] | None:
        return self.store.get(business_key)

    def receipt(self, business_key: str) -> dict[str, Any] | None:
        rec = self.store.get(business_key)
        return self._receipt(rec) if rec else None

    def _receipt(self, rec: dict[str, Any]) -> dict[str, Any]:
        """What is sent to the Resumate server. Sensitive keys are redacted."""
        send_result = self.receipt_includes_result and rec["status"] == "succeeded"
        return {
            "tool": rec["tool"],
            "idem_key": rec["provider_key"],
            "business_key": rec["business_key"],
            "status": rec["status"],
            "result": redact_value(rec["result"]) if send_result else None,
            "effect_type": rec["effect_type"],
            "attempts": rec["attempts"],
            "dedup_count": rec["dedup_count"],
        }

    # -- startup check and report ---
    def startup_check(self) -> SelfCheckReport:
        """
        Call once when the process starts, before running agent work:
        proves the store can write and read, warns when it is not durable,
        and turns entries left `processing` by a dead process into `unknown`.
        """
        store = self.store
        report = SelfCheckReport(
            ok=True,
            store=getattr(store, "name", type(store).__name__),
            durable=bool(getattr(store, "durable", False)),
            single_worker=bool(getattr(store, "single_worker", False)),
        )
        try:
            probe = self._new_record("__resumate_selfcheck__", "selfcheck", "none", attempt=1)
            store.delete("__resumate_selfcheck__")
            if not store.insert_if_absent(probe):
                raise RuntimeError("could not write the probe entry")
            if store.get("__resumate_selfcheck__") is None:
                raise RuntimeError("could not read back the probe entry")
            if not store.cas("__resumate_selfcheck__", ["processing"], status="succeeded"):
                raise RuntimeError("compare-and-set did not apply")
            store.delete("__resumate_selfcheck__")
        except Exception as exc:  # any store failure is a startup problem
            report.ok = False
            report.problems.append(f"store read/write check failed: {redact_text(str(exc))}")
            return report
        if not report.durable:
            report.warnings.append("store is not durable: a restart forgets every entry, so a duplicate is possible")
        now = self._clock()
        for rec in store.list(["processing"]):
            if rec["owner"] == self.owner:
                continue
            stuck = report.single_worker or (now - rec["claimed_at"]) >= self.stale_after
            if stuck and store.cas(rec["business_key"], ["processing"], status="unknown", error="stuck at startup"):
                report.marked_unknown.append(rec["business_key"])
        report.unknown_total = len(store.list(["unknown"]))
        if report.unknown_total:
            report.warnings.append(f"{report.unknown_total} effect(s) are unknown; see reconciliation_report()")
        return report

    def reconciliation_report(self) -> ReconciliationReport:
        """Every entry whose outcome is not known. These need a validator or a person."""
        now = self._clock()
        entries = []
        for rec in self.store.list(["unknown", "processing"]):
            if rec["status"] == "processing" and not self._is_stuck(rec, now):
                continue
            entries.append(
                {
                    "business_key": rec["business_key"],
                    "tool": rec["tool"],
                    "effect_type": rec["effect_type"],
                    "status": rec["status"],
                    "attempts": rec["attempts"],
                    "age_seconds": max(0.0, now - rec["claimed_at"]),
                    "error": rec["error"],
                }
            )
        return ReconciliationReport(generated_at=now, entries=entries)

    def resolve(self, business_key: str, answer: str, result: Any = None) -> dict[str, Any]:
        """
        Record a decision about an `unknown` entry (made by a person or a
        validator). effect_done -> succeeded, keeping `result`;
        effect_absent -> failed (confirmed not to have happened, may run again).
        """
        if answer not in (EFFECT_DONE, EFFECT_ABSENT):
            raise ValueError("answer must be 'effect_done' or 'effect_absent'")
        updates = {"status": "succeeded", "result": result, "error": None} if answer == EFFECT_DONE else {
            "status": "failed",
            "error": "confirmed absent",
        }
        if not self.store.cas(business_key, ["unknown", "processing"], **updates):
            raise ValueError(f"{business_key!r} is not unknown or processing")
        return self._receipt(self.store.get(business_key))

    # -- running ---
    def run(
        self,
        business_key: str,
        tool: str,
        handler: Callable[..., Any],
        *,
        effect_type: str,
        validator: Callable[[], Any] | None = None,
        rejected_exceptions: tuple[type[BaseException], ...] = (),
    ) -> tuple[Any, dict[str, Any]]:
        """
        Run `handler` at most once per business key. Returns (result, receipt).
        Raises EffectUnknown / EffectInProgress (never calls the handler in
        those cases) or whatever the handler raised, with `.side_effect_receipt`
        attached to the exception so Resumate can report it.
        """
        _check_effect_type(effect_type)
        step = self._claim(business_key, tool, effect_type)
        if step[0] == "validate":
            answer, result = _parse_answer(self._call_validator(validator, business_key))
            step = self._after_validation(step[1], answer, result, business_key, tool, effect_type)
        if step[0] == "return":
            return step[1]["result"], self._receipt(step[1])
        rec = step[1]
        try:
            result = self._call_handler(handler, rec)
        except BaseException as exc:
            raise self._fail(rec, exc, rejected_exceptions)
        done = self._succeed(rec, result)
        return result, self._receipt(done)

    async def arun(
        self,
        business_key: str,
        tool: str,
        handler: Callable[..., Any],
        *,
        effect_type: str,
        validator: Callable[[], Any] | None = None,
        rejected_exceptions: tuple[type[BaseException], ...] = (),
    ) -> tuple[Any, dict[str, Any]]:
        """Async twin of run(). The handler and validator may be sync or async."""
        _check_effect_type(effect_type)
        step = await asyncio.to_thread(self._claim, business_key, tool, effect_type)
        if step[0] == "validate":
            raw = await self._acall_validator(validator, business_key)
            answer, result = _parse_answer(raw)
            step = await asyncio.to_thread(self._after_validation, step[1], answer, result, business_key, tool, effect_type)
        if step[0] == "return":
            return step[1]["result"], self._receipt(step[1])
        rec = step[1]
        try:
            value = self._call_handler(handler, rec)
            if inspect.isawaitable(value):
                value = await value
        except BaseException as exc:
            raise await asyncio.to_thread(self._fail, rec, exc, rejected_exceptions)
        done = await asyncio.to_thread(self._succeed, rec, value)
        return value, self._receipt(done)

    # -- internals ---
    def _new_record(self, key: str, tool: str, effect_type: str, attempt: int) -> dict[str, Any]:
        now = self._clock()
        return {
            "business_key": key,
            "tool": tool,
            "effect_type": effect_type,
            "status": "processing",
            "provider_key": self.provider_key(key),
            "owner": self.owner,
            "attempts": attempt,
            "dedup_count": 0,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "claimed_at": now,
        }

    def _is_stuck(self, rec: dict[str, Any], now: float) -> bool:
        if rec["owner"] == self.owner:
            return False
        return (now - rec["claimed_at"]) >= self.stale_after

    def _claim(self, key: str, tool: str, effect_type: str):
        """Returns ("execute", rec), ("return", rec) or ("validate", rec). May raise."""
        for _ in range(5):
            if self.store.insert_if_absent(self._new_record(key, tool, effect_type, attempt=1)):
                return "execute", self.store.get(key)
            rec = self.store.get(key)
            if rec is None:
                continue
            status = rec["status"]
            if status == "succeeded":
                self.store.bump_dedup(key)
                return "return", self.store.get(key)
            if status == "processing" and self._is_stuck(rec, self._clock()):
                self.store.cas(key, ["processing"], status="unknown", error="stuck: owner stopped")
                continue
            if status == "processing":
                raise EffectInProgress(f"{key!r} is being processed right now", self._receipt(rec))
            rerun_ok = status == "failed" or (status == "unknown" and effect_type in _RERUN_SAFE)
            if rerun_ok:
                if self.store.cas(
                    key,
                    [status],
                    status="processing",
                    owner=self.owner,
                    claimed_at=self._clock(),
                    attempts=rec["attempts"] + 1,
                    error=None,
                ):
                    return "execute", self.store.get(key)
                continue
            return "validate", rec  # unknown and effectful
        raise EffectInProgress(f"{key!r} changed repeatedly while claiming", self._receipt(self.store.get(key)))

    def _after_validation(self, rec, answer, result, key, tool, effect_type):
        if answer == EFFECT_DONE:
            if not self.store.cas(key, ["unknown"], status="succeeded", result=result, error=None):
                return self._claim(key, tool, effect_type)
            return "return", self.store.get(key)
        if answer == EFFECT_ABSENT:
            if self.store.cas(
                key,
                ["unknown"],
                status="processing",
                owner=self.owner,
                claimed_at=self._clock(),
                attempts=rec["attempts"] + 1,
                error=None,
            ):
                return "execute", self.store.get(key)
            return self._claim(key, tool, effect_type)
        raise EffectUnknown(
            f"{key!r}: it is not known whether this effect happened and the validator could not tell",
            self._receipt(rec),
        )

    def _call_validator(self, validator, key):
        if validator is None:
            return UNCLEAR
        try:
            value = validator()
        except Exception:
            logger.exception("resumate_sdk: validator for %s raised; treating the answer as unclear", key)
            return UNCLEAR
        if inspect.isawaitable(value):
            raise TypeError("validator is async; use arun() instead of run()")
        return value

    async def _acall_validator(self, validator, key):
        if validator is None:
            return UNCLEAR
        try:
            value = validator()
            if inspect.isawaitable(value):
                value = await value
        except Exception:
            logger.exception("resumate_sdk: validator for %s raised; treating the answer as unclear", key)
            return UNCLEAR
        return value

    @staticmethod
    def _call_handler(handler, rec):
        if _wants_call(handler):
            return handler(Call(rec["business_key"], rec["provider_key"], rec["attempts"], rec["tool"]))
        return handler()

    def _succeed(self, rec: dict[str, Any], result: Any) -> dict[str, Any]:
        key = rec["business_key"]
        # `unknown` is allowed too: a stale-marker may have flipped the entry while the call ran.
        if not self.store.cas(key, ["processing", "unknown"], status="succeeded", result=result, error=None):
            logger.warning("resumate_sdk: could not record success for %s (entry changed state)", key)
        return self.store.get(key) or {**rec, "status": "succeeded", "result": result}

    def _fail(self, rec: dict[str, Any], exc: BaseException, extra_rejected) -> BaseException:
        """Record the outcome and return the exception to raise (with the receipt attached)."""
        key = rec["business_key"]
        rejected = isinstance(exc, (*self.rejected_exceptions, *extra_rejected)) and isinstance(exc, Exception)
        status = "failed" if rejected else "unknown"
        error = redact_text(f"{type(exc).__name__}: {exc}")
        self.store.cas(key, ["processing", "unknown"], status=status, error=error)
        current = self.store.get(key) or {**rec, "status": status, "error": error}
        try:
            exc.side_effect_receipt = self._receipt(current)  # type: ignore[attr-defined]
        except Exception:  # some exception types refuse new attributes
            logger.warning("resumate_sdk: could not attach a receipt to %s", type(exc).__name__)
        return exc


def _check_effect_type(effect_type: str) -> None:
    if effect_type not in EFFECT_TYPES:
        raise ValueError(f"effect_type must be one of {EFFECT_TYPES}, got {effect_type!r}")


def _parse_answer(raw: Any) -> tuple[str, Any]:
    """A validator returns an answer string, or (answer, result). Anything else is unclear."""
    result = None
    if isinstance(raw, tuple) and len(raw) == 2:
        raw, result = raw
    if raw in VALIDATOR_ANSWERS:
        return raw, result
    return UNCLEAR, None


def _wants_call(handler: Callable[..., Any]) -> bool:
    try:
        params = inspect.signature(handler).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty for p in params
    )
