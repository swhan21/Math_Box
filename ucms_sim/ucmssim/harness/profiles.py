"""고장 주입·운전 프로파일 (플랜트 오버라이드용 t → 값 함수)."""

from __future__ import annotations

import math
import random
from itertools import pairwise
from typing import Optional, Sequence

from ..plant import Profile

Point = tuple[float, float]


def piecewise(points: Sequence[Point]) -> Profile:
    """(t, v) 점들을 직선 보간한다. 마지막 점 이후에는 None (오버라이드 종료)."""
    pts = list(points)

    def profile(t: float) -> Optional[float]:
        if t > pts[-1][0]:
            return None
        if t <= pts[0][0]:
            return pts[0][1]
        for (t0, v0), (t1, v1) in pairwise(pts):
            if t <= t1:
                return v1 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return pts[-1][1]

    return profile


def ramp_hold_return(t: float, start: float, peak: float, rate: float, hold_s: float, end: float) -> list[Point]:
    """start → peak (rate/s) → hold_s 유지 → end (rate/s)."""
    t1 = t + abs(peak - start) / rate
    t2 = t1 + hold_s
    t3 = t2 + abs(peak - end) / rate
    return [(t, start), (t1, peak), (t2, peak), (t3, end)]


def crossing_time(points: Sequence[Point], level: float) -> float | None:
    """프로파일이 level을 처음 넘는(가로지르는) 시각 — 조건 성립 시각 t0."""
    for (t0, v0), (t1, v1) in pairwise(points):
        if (v0 - level) * (v1 - level) <= 0 and v0 != v1:
            return t0 + (level - v0) / (v1 - v0) * (t1 - t0)
    return None


def oscillation(
    center: float, amplitude: float, period: float, t_start: float, duration: float,
    settle_to: float, settle_rate: float,
) -> Profile:
    """center ± amplitude 사인 진동 → 종료 후 settle_to 까지 settle_rate/s 로 하강 → None."""
    t_end = t_start + duration
    v_end = center + amplitude * math.sin(2 * math.pi * duration / period)

    def profile(t: float) -> Optional[float]:
        if t <= t_end:
            return center + amplitude * math.sin(2 * math.pi * (t - t_start) / period)
        v = v_end - settle_rate * (t - t_end)
        return v if v > settle_to else None

    return profile


def drive_cycle(rng: random.Random, duration_s: float) -> list[tuple[float, float]]:
    """정상 운전 속도 지령 일정 [(시각 오프셋, km/h)] — 순항·감속·정차 반복 (FI-11)."""
    plan, t = [], 0.0
    while t < duration_s:
        for kph, hold in ((30.0, 60.0), (15.0, 40.0), (0.0, 20.0)):
            plan.append((t, max(0.0, kph + rng.uniform(-3.0, 3.0)) if kph else 0.0))
            t += hold * rng.uniform(0.8, 1.2)
    return plan
