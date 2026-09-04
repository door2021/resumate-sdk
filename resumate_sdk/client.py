from __future__ import annotations

import logging
import random
import time
from typing import Any

import httpx

from .exceptions import ResumateAPIError, ResumateAPIRejected, ResumateAPIUnavailable

logger = logging.getLogger("resumate_sdk")

# 429 (rate limited) and 5xx (server-side) are worth retrying.
# 4xx other than 429 means the request itself is wrong - retrying won't help.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ResumateClient:
    """
    Thin HTTP client for the Resumate checkpoint API, with retry/backoff
    and a deliberate fail-open default: see report_step's docstring for
    why a Resumate outage should not, by default, crash your agent run.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.resumate.dev",
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        fail_open: bool = True,
        transport: httpx.BaseTransport | None = None,  # test injection point
    ):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.fail_open = fail_open

    # --- core retry machinery ---------------------------------------------
    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt, exc)
                    continue
                raise ResumateAPIUnavailable(
                    f"Could not reach Resumate API after {self.max_retries + 1} attempts: {exc}"
                ) from exc

            if resp.status_code in _RETRYABLE_STATUS_CODES:
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} from Resumate API", request=resp.request, response=resp
                )
                if attempt < self.max_retries:
                    self._sleep_backoff(attempt, last_exc, retry_after=resp.headers.get("Retry-After"))
                    continue
                raise ResumateAPIUnavailable(
                    f"Resumate API returned {resp.status_code} after {self.max_retries + 1} attempts"
                ) from last_exc

            if resp.status_code >= 400:
                # Non-retryable client error (bad payload, bad auth, etc).
                raise ResumateAPIRejected(
                    f"Resumate API rejected the request: {resp.status_code} {resp.text[:200]}"
                )

            return resp

        # Unreachable in practice (loop always returns or raises), but keeps
        # type-checkers and readers honest about the contract.
        raise ResumateAPIUnavailable("Resumate API request failed") from last_exc

    def _sleep_backoff(self, attempt: int, exc: Exception, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self._backoff_delay(attempt)
        else:
            delay = self._backoff_delay(attempt)
        logger.warning(
            "resumate_sdk: request failed (attempt %d/%d), retrying in %.2fs: %s",
            attempt + 1,
            self.max_retries + 1,
            delay,
            exc,
        )
        time.sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self.backoff_base * (2**attempt), self.backoff_max)
        return base + random.uniform(0, base * 0.1)  # jitter, avoids thundering herd

    # --- public API ---------------------------------------------------------
    def report_step(
        self,
        agent_name: str,
        run_external_id: str,
        step_index: int,
        step_name: str,
        status: str,
        output: dict[str, Any] | None = None,
        error: dict[str, str] | None = None,
        run_status: str | None = None,
    ) -> dict[str, Any]:
        """
        Report a step's outcome. FAIL-OPEN BY DEFAULT: if the Resumate API
        is unreachable after retries, this logs a warning and returns
        {"step_recorded": False, ...} instead of raising - your node's own
        successful work should not be undone by our infrastructure being
        down. Set fail_open=False on the client if you'd rather this raise
        ResumateAPIError instead (e.g. you want checkpointing gaps to be a
        hard stop, not a silent miss).
        """
        payload = {
            "agent_name": agent_name,
            "run_external_id": run_external_id,
            "step_index": step_index,
            "step_name": step_name,
            "status": status,
            "output": output,
            "error": error,
            "run_status": run_status,
        }
        try:
            resp = self._request_with_retry("POST", "/api/v1/checkpoints/", json=payload)
            return resp.json()
        except ResumateAPIError as exc:
            if self.fail_open:
                logger.error(
                    "resumate_sdk: failed to report step %d (%s) for run %s after retries, "
                    "continuing without a checkpoint (fail_open=True): %s",
                    step_index,
                    step_name,
                    run_external_id,
                    exc,
                )
                return {"step_recorded": False, "error": str(exc)}
            raise

    def get_resume(self, run_external_id: str) -> dict[str, Any]:
        resp = self._request_with_retry("GET", f"/api/v1/runs/{run_external_id}/resume/")
        return resp.json()

    def consume_resume(self, run_external_id: str) -> None:
        self._request_with_retry("POST", f"/api/v1/runs/{run_external_id}/resume/consume/")

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ResumateClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
