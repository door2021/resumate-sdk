class ResumateAPIError(Exception):
    """
    Base class for errors raised by the Resumate SDK talking to the API,
    after retries are exhausted. Distinct from exceptions raised by your
    own agent/node code, so you can catch this specifically if you want
    to react to Resumate itself being degraded (e.g. alert, or switch to
    fail_open=False behavior deliberately).
    """


class ResumateAPIUnavailable(ResumateAPIError):
    """Connection/timeout failures reaching the API at all (as opposed to
    a request that reached the server but the server rejected)."""


class ResumateAPIRejected(ResumateAPIError):
    """The API reached and responded, but with a non-retryable error
    (e.g. 400 bad payload, 401 bad API key). Retrying won't help."""
