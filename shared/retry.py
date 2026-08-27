"""Retry with exponential backoff and jitter (P2-8)."""
from __future__ import annotations

import functools
import logging
import random
import time
from typing import Any, Callable, Optional, Sequence, Type

logger = logging.getLogger("shared.retry")


def retry_with_backoff(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retry_exceptions: Sequence[Type[Exception]] = (Exception,),
    retry_predicate: Optional[Callable[[Exception], bool]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Execute callable with exponential backoff on retriable errors."""
    delay = initial_delay
    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            is_retriable_type = any(isinstance(e, exc_t) for exc_t in retry_exceptions)
            if not is_retriable_type:
                raise

            if retry_predicate is not None and not retry_predicate(e):
                raise

            if attempt == max_retries:
                logger.error("All %d retry attempts exhausted: %s", max_retries, e)
                raise

            actual_delay = delay
            if jitter:
                actual_delay = delay * (0.8 + 0.4 * random.random())

            logger.warning(
                "Attempt %d/%d failed with %s (%s); retrying in %.2fs",
                attempt + 1, max_retries, type(e).__name__, e, actual_delay
            )
            sleep_fn(actual_delay)
            delay = min(delay * backoff_factor, max_delay)

    if last_exc is not None:
        raise last_exc


def retry(
    max_retries: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retry_exceptions: Sequence[Type[Exception]] = (Exception,),
    retry_predicate: Optional[Callable[[Exception], bool]] = None,
):
    """Decorator for functions requiring exponential backoff."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return retry_with_backoff(
                fn,
                *args,
                max_retries=max_retries,
                initial_delay=initial_delay,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
                jitter=jitter,
                retry_exceptions=retry_exceptions,
                retry_predicate=retry_predicate,
                **kwargs,
            )
        return wrapper
    return decorator
