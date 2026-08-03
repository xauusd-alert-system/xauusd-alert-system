"""Poisson event injector producing market news shocks.

The injector schedules discrete "news events" on a :class:`SimClock`
using a Poisson arrival process (average inter-arrival
``news_mean_arrival_ticks`` from the simulation config).  Each event
applies a zero-mean Gaussian shock to the ``news_shock`` value read by
agents (notably the :class:`FundamentalAgent`), and the shock decays
geometrically across subsequent ticks until the next event arrives.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Optional

from simulation.environment.clock import SimClock


class NewsInjector:
    """Discrete-event news source.

    Example::

        injector = NewsInjector(cfg)
        injector.attach(clock)
        # ... engine loop ...
        shock = injector.news_shock   # current decaying shock
    """

    def __init__(
        self,
        cfg: dict,
        rng: Optional[random.Random] = None,
        clock: Optional[SimClock] = None,
    ) -> None:
        self.cfg = cfg
        self.rng = rng or random.Random()
        self.clock = clock

        self.mean_arrival_ticks: float = float(
            cfg.get("news_mean_arrival_ticks", 500.0)
        )
        self.shock_std: float = float(cfg.get("news_shock_std", 0.002))
        self._decay: float = 0.9
        if self.mean_arrival_ticks > 0:
            # Geometric arrivals with the given mean.
            self._arrival_prob: float = 1.0 / self.mean_arrival_ticks
        else:
            self._arrival_prob = 0.0

        self._current_shock: float = 0.0
        self.total_events: int = 0
        if clock is not None:
            self.attach(clock)

    # --- lifecycle ------------------------------------------------------

    def attach(self, clock: SimClock) -> None:
        """Attach to a clock and (re)arm the Poisson re-scheduler."""
        self.clock = clock
        if self._arrival_prob > 0.0:
            # Fire the next arrival check at a random geometric offset so
            # events do not bunch deterministically at bar boundaries.
            first_in = max(1, int(self.rng.expovariate(self._arrival_prob)))
            clock.schedule_every(
                interval=1,
                callback=self._check_arrival,
                priority=-10,  # news fires before normal agent actions
                start_in=first_in,
                max_repeats=None,
            )

    def step(self, dt_ticks: int = 1) -> None:
        """Directly decay the shock (when not driven by a clock)."""
        if self.clock is not None:
            # Managed by the clock; skip manual stepping to avoid double-decay.
            return
        if dt_ticks > 0:
            self._decay_shock(dt_ticks)

    # --- internal -------------------------------------------------------

    def _check_arrival(self, tick: int, ordinal: int) -> None:
        # SimClock.schedule_every invokes repeating callbacks as
        # callback(tick, ordinal); ordinal is a monotonically increasing
        # recurrence counter that this check does not need.
        self._decay_shock(1)

        if self._arrival_prob > 0.0 and self.rng.random() < self._arrival_prob:
            self._emit_event(tick)

    def _emit_event(self, tick: int) -> None:
        magnitude = abs(self.rng.gauss(0.0, self.shock_std))
        # Randomly pick direction sign so shocks are symmetric in expectation.
        self._current_shock = self.rng.choice((-1.0, 1.0)) * magnitude
        self.total_events += 1

    def _decay_shock(self, ticks: int) -> None:
        if self._current_shock != 0.0:
            self._current_shock *= self._decay ** ticks
            if abs(self._current_shock) < 1e-12:
                self._current_shock = 0.0

    # --- public accessors -----------------------------------------------

    @property
    def news_shock(self) -> float:
        """Current (decaying) news shock, used in agent context."""
        return self._current_shock

    @property
    def shocked(self) -> bool:
        """True when a fresh news shock is active."""
        return self._current_shock != 0.0

    def reset(self) -> None:
        self._current_shock = 0.0
        self.total_events = 0


# Backwards-compatible functional helper used by the simulator for a simple
# inline probability check without a dedicated clock callback.
def should_emit_news(cfg: dict, rng: random.Random) -> bool:
    """Cheap per-tick Poisson check (used when not driven by SimClock)."""
    mean = float(cfg.get("news_mean_arrival_ticks", 500.0))
    if mean <= 0.0:
        return False
    return rng.random() < (1.0 / mean)


def decay_factor() -> float:
    return 0.9
