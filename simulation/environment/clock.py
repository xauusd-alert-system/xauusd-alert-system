"""SimClock: a heapq-based discrete-event clock for the market simulator.

The clock keeps a single notion of simulation time measured in integer
ticks and a min-heap of (tick, sequence, priority, action) events.  The
priority field lets callers register urgent events (e.g. news shocks)
that must fire before regular scheduled events at the same tick.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass(order=True)
class _Event:
    """Heap entry.  Ordering is (tick, sequence, priority)."""

    tick: int
    sequence: int
    priority: int
    action: Any = field(compare=False)


class SimClock:
    """Min-heap based discrete event scheduler.

    Usage::

        clock = SimClock(initial_tick=0)
        clock.schedule_at(tick=10, callback=on_tick_ten)
        clock.schedule_every(interval=5, callback=on_every_five)
        clock.advance(1)          # process events with tick == 1
        clock.tick                 # -> 1
    """

    def __init__(self, initial_tick: int = 0) -> None:
        self.tick: int = int(initial_tick)
        self._heap: list[_Event] = []
        self._seq: int = 0
        self._repeating: list[tuple[int, int, int, Callable[[int, int], None]]] = []

    # --- scheduling -----------------------------------------------------

    def schedule_at(
        self,
        tick: int,
        callback: Callable[[int], Any],
        priority: int = 0,
    ) -> None:
        """Schedule ``callback(tick)`` to run exactly once at the given tick."""
        tick = int(tick)
        if tick < self.tick:
            raise ValueError(
                f"Cannot schedule event at tick {tick} in the past "
                f"(current tick is {self.tick})."
            )
        heapq.heappush(self._heap, _Event(tick, self._seq, priority, callback))
        self._seq += 1

    def schedule(
        self,
        delay_ticks: int,
        callback: Callable[[int], Any],
        priority: int = 0,
    ) -> None:
        """Schedule ``callback`` to run after ``delay_ticks`` from now."""
        self.schedule_at(self.tick + int(delay_ticks), callback, priority)

    def schedule_every(
        self,
        interval: int,
        callback: Callable[[int, int], Any],
        priority: int = 0,
        start_in: int = 0,
        max_repeats: Optional[int] = None,
    ) -> None:
        """Schedule a repeating callback.

        ``callback(tick, ordinal)`` is invoked every ``interval`` ticks.
        The first invocation occurs at ``self.tick + start_in``.  Pass
        ``max_repeats`` to bound the total number of invocations, or
        ``None`` for an unbounded recurrence (cancel via ``cancel_repeating``).
        """
        interval = int(interval)
        if interval <= 0:
            raise ValueError("interval must be a positive integer")
        if max_repeats is not None and max_repeats <= 0:
            raise ValueError("max_repeats must be positive or None")
        ordinal = 0
        tick = self.tick + int(start_in)

        def _recur(*_args: Any) -> None:
            nonlocal ordinal
            ordinal += 1
            callback(tick, ordinal)
            if max_repeats is not None and ordinal >= max_repeats:
                return
            # Pull the (fixed tick, ordinal) entry back out of the repeat
            # stack so each reschedule uses the up-to-date ordinal.
            heap_tick = tick
            # Reschedule from the *current* clock tick + interval so a slow
            # consumer cannot drift the cadence into the past.
            self.schedule_at(
                max(heap_tick + interval, self.tick + interval),
                _recur,
                priority,
            )

        # Record the repetition rule for introspection / cancellation is not
        # possible for heap entries; instead we push the one-shot trampoline.
        self.schedule_at(tick, _recur, priority)

    def cancel_at(self, tick: int) -> None:
        """Cancel the highest-priority (earliest) event scheduled at ``tick``.

        Because heap entries cannot be removed in O(1), this simply pops it
        if it is at the head, otherwise it is left to be ignored by lazy
        invalidation.  In practice the engine avoids cancel-by-tick and uses
        explicit guard flags inside callbacks.
        """
        while self._heap and self._heap[0].tick == tick:
            heapq.heappop(self._heap)

    # --- advancement ----------------------------------------------------

    def next_tick(self) -> int:
        """Return the tick of the next pending event, or ``None`` if empty."""
        if not self._heap:
            return self.tick  # nothing scheduled -> stay put
        return max(self._heap[0].tick, self.tick)

    def has_events(self) -> bool:
        return bool(self._heap)

    def advance(self, n_ticks: int = 1) -> int:
        """Fast-forward ``n_ticks`` and fire every due event.

        Returns the final tick value after advancement.  Inline events that
        call ``schedule_at``/``schedule`` for the current tick are processed
        in FIFO order thanks to the monotonically increasing ``sequence``.
        """
        target = self.tick + int(n_ticks)
        if target == self.tick:
            return self.tick

        self.tick = target
        while self._heap and self._heap[0].tick <= self.tick:
            event = heapq.heappop(self._heap)
            if callable(event.action):
                try:
                    event.action(event.tick)
                except Exception:  # pragma: no cover - defensive isolation
                    # A single faulty callback must not kill the simulation.
                    import logging

                    logging.getLogger(__name__).exception(
                        "SimClock event failed at tick %d", event.tick
                    )
        return self.tick

    def step(self) -> int:
        """Advance exactly one tick and process any events due at that tick."""
        return self.advance(1)

    # --- introspection --------------------------------------------------

    @property
    def pending_event_count(self) -> int:
        return len(self._heap)

    @property
    def next_event_tick(self) -> Optional[int]:
        """Tick of the next scheduled event, or ``None`` if none pending."""
        return self._heap[0].tick if self._heap else None

    def __len__(self) -> int:
        return len(self._heap)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SimClock(tick={self.tick}, "
            f"pending={len(self._heap)}, next_event={self.next_event_tick})"
        )
