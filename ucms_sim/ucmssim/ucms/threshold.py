"""Threshold Evaluator: 임계값 + 히스테리시스 판정 (설계안 6.4절, 6.5절 상태도).

    Normal   → Attack    : 한계 초과 1회
    Attack   → Normal    : 복귀
    Attack   → Exceeded  : 연속 N회 초과 (attack)          → EXCEEDED 이벤트
    Exceeded → Release   : 해제 구간 이내 1회
    Release  → Exceeded  : 재초과 (해제 구간 밖)
    Release  → Normal    : 연속 M회 해제 구간 이내 (release) → RELEASED 이벤트

해제 구간 = 한계값에서 불감대(hysteresis)만큼 안쪽 (편차 DEV-009).
임계값 초과 이벤트는 건전성(Monitored_Resource)에 영향을 주지 않는다 (부록 A.4.4).
"""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import Any, Callable

from ..topics import (
    CharacteristicValue,
    ExceedanceState,
    LimitType,
    MonitoredAttributeSpecification,
    SpecProvider,
    ThresholdExceedanceEvent,
)
from .persistence import UcmsStore
from .spec_registry import SpecRegistry


class MonitorState(str, Enum):
    NORMAL = "Normal"
    ATTACK = "Attack"
    EXCEEDED = "Exceeded"
    RELEASE = "Release"


class LimitMonitor:
    """한계값 하나(상한 또는 하한)에 대한 attack/release 판정기. DDS·DB와 무관한 순수 로직."""

    def __init__(
        self, limit_type: LimitType, limit: float, hysteresis: float = 0.0, attack: int = 1, release: int = 1
    ) -> None:
        if attack < 1 or release < 1:
            raise ValueError("attack/release must be >= 1")
        self.limit_type = limit_type
        self.limit = limit
        self.hysteresis = hysteresis
        self.attack = attack
        self.release = release
        self.state = MonitorState.NORMAL
        self._count = 0

    def _beyond(self, v: float) -> bool:
        return v > self.limit if self.limit_type is LimitType.MAX else v < self.limit

    def _within(self, v: float) -> bool:
        if self.limit_type is LimitType.MAX:
            return v <= self.limit - self.hysteresis
        return v >= self.limit + self.hysteresis

    def force_exceeded(self) -> None:
        """재기동 복원: 활성 초과 이벤트가 있으면 Exceeded 상태에서 시작한다."""
        self.state = MonitorState.EXCEEDED
        self._count = 0

    def update(self, v: float) -> ExceedanceState | None:
        s = self.state
        if s in (MonitorState.NORMAL, MonitorState.ATTACK):
            if not self._beyond(v):
                self.state, self._count = MonitorState.NORMAL, 0
                return None
            self._count = self._count + 1 if s is MonitorState.ATTACK else 1
            if self._count >= self.attack:
                self.state, self._count = MonitorState.EXCEEDED, 0
                return ExceedanceState.EXCEEDED
            self.state = MonitorState.ATTACK
            return None

        if not self._within(v):
            self.state, self._count = MonitorState.EXCEEDED, 0
            return None
        self._count = self._count + 1 if s is MonitorState.RELEASE else 1
        if self._count >= self.release:
            self.state, self._count = MonitorState.NORMAL, 0
            return ExceedanceState.RELEASED
        self.state = MonitorState.RELEASE
        return None


MonitorKey = tuple[str, str, LimitType]


class ThresholdEvaluator:
    def __init__(
        self,
        store: UcmsStore,
        registry: SpecRegistry,
        publish: Callable[[Any], None],
        dispose: Callable[[Any], None],
        now: Callable[[], float],
        log: Callable[[str, str], None],
    ) -> None:
        self.store = store
        self.registry = registry
        self.publish = publish
        self.dispose = dispose
        self.now = now
        self.log = log
        self.active: dict[MonitorKey, ThresholdExceedanceEvent] = {}
        self.unevaluated = 0                            # 사양 채택 전 수신 샘플 수
        self._monitors: dict[MonitorKey, tuple[tuple, LimitMonitor]] = {}

    def restore(self) -> None:
        for ev in self.store.active_threshold_events():
            self.active[(ev.resource_id, ev.characteristic_id, ev.limit_type)] = ev
            self.publish(ev)
        if self.active:
            self.log("INFO", f"활성 임계값 이벤트 {len(self.active)}건 복원")

    def on_value(self, cv: CharacteristicValue) -> None:
        adopted = self.registry.attribute_spec(cv.resource_id, cv.characteristic_id)
        if adopted is None:
            self.unevaluated += 1
            return
        spec, provider = adopted
        for limit_type, limit in ((LimitType.MAX, spec.max_limit), (LimitType.MIN, spec.min_limit)):
            if limit is None:
                continue
            key = (cv.resource_id, cv.characteristic_id, limit_type)
            out = self._monitor(key, spec, provider, limit).update(cv.value)
            if out is ExceedanceState.EXCEEDED:
                ev = ThresholdExceedanceEvent(
                    event_id=self.store.next_event_id("TE"),
                    resource_id=cv.resource_id,
                    characteristic_id=cv.characteristic_id,
                    limit_type=limit_type,
                    limit_value=limit,
                    trigger_value=cv.value,
                    release_value=None,
                    state=ExceedanceState.EXCEEDED,
                    spec_provider=provider,
                    exceeded_time=self.now(),
                    released_time=None,
                    source_timestamp=cv.timestamp,
                )
                self.active[key] = ev
                self._emit(ev)
            elif out is ExceedanceState.RELEASED:
                cur = self.active.pop(key, None)
                if cur is not None:
                    ev = replace(
                        cur, state=ExceedanceState.RELEASED, release_value=cv.value,
                        released_time=self.now(), source_timestamp=cv.timestamp,
                    )
                    self._emit(ev)
                    self.dispose(ev)

    def _monitor(
        self, key: MonitorKey, spec: MonitoredAttributeSpecification, provider: SpecProvider, limit: float
    ) -> LimitMonitor:
        signature = (limit, spec.hysteresis, spec.attack_samples, spec.release_samples, provider)
        entry = self._monitors.get(key)
        if entry is not None and entry[0] == signature:
            return entry[1]
        mon = LimitMonitor(key[2], limit, spec.hysteresis, spec.attack_samples, spec.release_samples)
        if key in self.active:
            mon.force_exceeded()
        self._monitors[key] = (signature, mon)
        return mon

    def _emit(self, ev: ThresholdExceedanceEvent) -> None:
        self.store.record_threshold_event(self.now(), ev)  # 저장 후 발행
        self.publish(ev)
