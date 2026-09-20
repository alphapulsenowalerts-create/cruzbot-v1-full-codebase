"""Rate-limit aware retry, exponential backoff, and reconnect helpers."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Type, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryableError(Exception):
    """Errors that should trigger a retry (429, disconnect, timeout)."""


class RateLimitError(RetryableError):
    def __init__(self, message: str = "rate limited", retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class ExponentialBackoff:
    """Stateful exponential backoff with jitter."""

    base: float = 0.5
    factor: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.25
    attempt: int = 0

    def next_delay(self, retry_after: Optional[float] = None) -> float:
        if retry_after is not None and retry_after > 0:
            delay = float(retry_after)
        else:
            delay = min(self.base * (self.factor ** self.attempt), self.max_delay)
            delay *= 1.0 + random.uniform(-self.jitter, self.jitter)
        self.attempt += 1
        return max(0.0, delay)

    def reset(self) -> None:
        self.attempt = 0


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 5,
    backoff: Optional[ExponentialBackoff] = None,
    retry_on: tuple[Type[BaseException], ...] = (RetryableError, asyncio.TimeoutError, ConnectionError, OSError),
    on_retry: Optional[Callable[[int, BaseException, float], None]] = None,
) -> T:
    """Execute async `fn` with exponential backoff on retryable failures."""
    bo = backoff or ExponentialBackoff()
    last_exc: Optional[BaseException] = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await fn()
            bo.reset()
            return result
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= max_attempts:
                break
            retry_after = getattr(exc, "retry_after", None)
            delay = bo.next_delay(retry_after)
            if on_retry:
                on_retry(attempt, exc, delay)
            else:
                logger.warning(
                    "retry %s/%s after %s: %s (sleep %.2fs)",
                    attempt,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                    delay,
                )
            await asyncio.sleep(delay)
        except Exception:
            raise

    assert last_exc is not None
    raise last_exc


def is_rate_limit_status(status: Union[int, str]) -> bool:
    try:
        return int(status) == 429
    except (TypeError, ValueError):
        return False
