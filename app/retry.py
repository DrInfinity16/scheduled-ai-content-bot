"""Bounded retries with exponential backoff for transient failures.

This module is stdlib-only on purpose: integrations (Notion, X, Gemini)
import it, so it must never import them. Classification is duck-typed:
HTTP status codes, well-known exception names and the ``__cause__`` chain
are enough to separate *transient* from *permanent* failures without
coupling the retry policy to any SDK.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 30.0

# Network-level conditions worth retrying (HTTP semantics + timeouts).
TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Exception class names that signal a transient condition across SDKs
# (gRPC-style "ServiceUnavailable", requests "ReadTimeout", ...).
TRANSIENT_NAME_MARKERS = (
    "timeout",
    "timedout",
    "unavailable",
    "toomanyrequests",
    "ratelimit",
    "rate_limit",
    "temporarily",
    "temporary",
    "internalserver",
    "deadlineexceeded",
    "connection",
)

_MAX_CHAIN_DEPTH = 5


@dataclass(frozen=True)
class RetryPolicy:
    """``max_attempts`` total tries with delays 1s, 2s, 4s ... (capped)."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts debe ser >= 1")
        if self.base_delay < 0:
            raise ValueError("base_delay debe ser >= 0")
        if self.max_delay < 0:
            raise ValueError("max_delay debe ser >= 0")

    def delay_for(self, failed_attempt: int) -> float:
        """Delay after ``failed_attempt`` failed tries (1-based)."""
        if failed_attempt < 1:
            return 0.0
        return min(self.base_delay * (2 ** (failed_attempt - 1)), self.max_delay)


def _status_code_of(exc: BaseException) -> Optional[int]:
    for attribute in ("status_code", "code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_transient_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    code = _status_code_of(exc)
    if code is not None:
        return code in TRANSIENT_STATUS_CODES
    name = type(exc).__name__.lower()
    return any(marker in name for marker in TRANSIENT_NAME_MARKERS)


def is_transient(exc: BaseException) -> bool:
    """True only for failures worth retrying.

    Walks the ``__cause__`` / ``__context__`` chain (Gemini wraps SDK
    errors in ``GenerationError``, Notion wraps sockets errors), decides on
    the first element that carries a status code or a known transient name,
    and defaults to **permanent** for anything unknown. 4xx-style failures
    (400/401/403/404/422), schema errors, validation errors and
    configuration errors therefore never retry.
    """
    current: Optional[BaseException] = exc
    depth = 0
    while current is not None and depth < _MAX_CHAIN_DEPTH:
        if _is_transient_exception(current):
            return True
        if _status_code_of(current) is not None:
            # A definite status code that is not transient: permanent.
            return False
        current = current.__cause__ or current.__context__
        depth += 1
    return False


def with_retries(
    operation: Callable[[], T],
    *,
    policy: Optional[RetryPolicy] = None,
    retry_if: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Run ``operation``, retrying transient failures up to the policy.

    Raises the last exception when attempts are exhausted or when the
    failure is permanent. ``on_retry`` makes every retry observable.
    """
    policy = policy or RetryPolicy()
    attempt = 0
    while True:
        attempt += 1
        try:
            return operation()
        except Exception as exc:
            if attempt >= policy.max_attempts or not retry_if(exc):
                raise
            delay = policy.delay_for(attempt)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            if delay > 0:
                sleep(delay)


__all__ = [
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "RetryPolicy",
    "is_transient",
    "with_retries",
]
