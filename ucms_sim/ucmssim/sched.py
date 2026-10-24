"""이산 사건(discrete-event) 스케줄러 — SIL 시뮬레이션의 단일 모의 시계.

설계안 5.4절은 시험 하니스가 t0(주입)·t1(수신)을 같은 PC 시계로 기록하도록 한다.
SIL에서는 모든 노드가 이 스케줄러의 시계 하나를 공유하므로 노드 간 동기 오차가 없다.
"""

from __future__ import annotations

import heapq
import itertools
import time
from typing import Callable


class Timer:
    """call_at / every 가 돌려주는 취소 핸들."""

    __slots__ = ("cancelled",)

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class Scheduler:
    """모의 시계 + 사건 큐.

    realtime_factor 를 주면 벽시계에 맞춰 진행한다 (1.0 = 실시간, 5.0 = 5배속).
    None 이면 최대 속도로 진행한다.
    """

    def __init__(self, realtime_factor: float | None = None) -> None:
        self._now = 0.0
        self._queue: list = []
        self._seq = itertools.count()
        self.realtime_factor = realtime_factor
        self._wall_origin: float | None = None

    @property
    def now(self) -> float:
        return self._now

    def call_at(self, t: float, fn: Callable, *args) -> Timer:
        timer = Timer()
        self._push(max(t, self._now), timer, fn, args)
        return timer

    def call_later(self, delay: float, fn: Callable, *args) -> Timer:
        return self.call_at(self._now + delay, fn, *args)

    def every(self, period: float, fn: Callable[[], None], start: float | None = None) -> Timer:
        """start + k*period 시각마다 fn 을 실행한다 (누적 오차 없음)."""
        if period <= 0:
            raise ValueError("period must be positive")
        timer = Timer()
        origin = self._now if start is None else max(start, self._now)

        def tick(k: int) -> None:
            fn()
            if not timer.cancelled:
                self._push(origin + (k + 1) * period, timer, tick, (k + 1,))

        self._push(origin, timer, tick, (0,))
        return timer

    def run_until(self, t_end: float) -> None:
        while self._queue and self._queue[0][0] <= t_end:
            self._step()
        self._pace(t_end)
        self._now = max(self._now, t_end)

    def run_for(self, duration: float) -> None:
        self.run_until(self._now + duration)

    def run_until_true(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """predicate 가 참이 되거나 timeout(모의 시간)이 지날 때까지 진행한다."""
        deadline = self._now + timeout
        if predicate():
            return True
        while self._queue and self._queue[0][0] <= deadline:
            if self._step() and predicate():
                return True
        self._pace(deadline)
        self._now = max(self._now, deadline)
        return predicate()

    def _push(self, t: float, timer: Timer, fn: Callable, args: tuple) -> None:
        heapq.heappush(self._queue, (t, next(self._seq), timer, fn, args))

    def _step(self) -> bool:
        t, _, timer, fn, args = heapq.heappop(self._queue)
        if timer.cancelled:
            return False
        self._pace(t)
        self._now = t
        fn(*args)
        return True

    def _pace(self, t: float) -> None:
        if not self.realtime_factor:
            return
        wall = time.monotonic()
        if self._wall_origin is None:
            self._wall_origin = wall - self._now / self.realtime_factor
        delay = self._wall_origin + t / self.realtime_factor - wall
        if delay > 0:
            time.sleep(delay)
