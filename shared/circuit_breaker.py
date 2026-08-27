"""Circuit Breaker pattern implementation (P2-7)."""
from __future__ import annotations

import enum
import time
from typing import Any, Callable, Optional


class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when an operation is attempted while circuit is OPEN."""
    pass


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        clock: Callable[[], float] = time.time,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.clock = clock
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.last_state_change: float = clock()

    @property
    def is_available(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        now = self.clock()
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.last_state_change = now
                return True
            return False
        return True  # HALF_OPEN allows probe call

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_state_change = self.clock()

    def record_failure(self) -> None:
        now = self.clock()
        self.failure_count += 1
        self.last_failure_time = now
        if self.state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            if self.failure_count >= self.failure_threshold or self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.last_state_change = now

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if not self.is_available:
            raise CircuitBreakerOpenError(
                f"Circuit breaker is OPEN (failed {self.failure_count} times; "
                f"recovers in {max(0.0, self.recovery_timeout - (self.clock() - self.last_failure_time)):.1f}s)"
            )
        try:
            res = fn(*args, **kwargs)
            self.record_success()
            return res
        except Exception as e:
            self.record_failure()
            raise e
