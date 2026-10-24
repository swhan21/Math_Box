"""간이 HMI (O-role 일부): 리소스 건전성, 활성 고장, 임계값 이벤트, 특성값 추이, 사용량 표시.

UCMS가 발행한 토픽과 리소스가 발행한 특성값·사용량을 구독한다. 상태 토픽이 TRANSIENT_LOCAL
이므로 늦게 접속해도 현재 상태를 즉시 표시한다 (FI-09). 래치 고장 운용자 해제 명령을 보낼 수 있다.

표시 방식은 두 가지다: 텍스트 화면 render() (데모·스냅샷), 상태 사전 snapshot() (웹 GUI).
"""

from __future__ import annotations

from collections import deque
from typing import Any

from .config import QosConfig
from .dds import DataWriter, Domain, Participant, SampleInfo
from .sched import Scheduler
from .topics import (
    CharacteristicUsage,
    CharacteristicValue,
    EventState,
    ExceedanceState,
    FaultClearCommand,
    FaultEvent,
    Health,
    MonitoredAttributeSpecification,
    MonitoredResource,
    MonitoredResourceSpecification,
    ThresholdExceedanceEvent,
    to_dict,
)

SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    if not values:
        return ""
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[0] * len(values)
    return "".join(SPARK[int((v - lo) / (hi - lo) * (len(SPARK) - 1))] for v in values)


class SimpleHmi:
    def __init__(
        self, sched: Scheduler, domain: Domain, qos: QosConfig, name: str = "HMI", trend_len: int = 40
    ) -> None:
        self.sched = sched
        self.domain = domain
        self.qos = qos
        self.name = name
        self.trend_len = trend_len
        self.participant: Participant | None = None
        self.resources: dict[str, MonitoredResource] = {}
        self.resource_specs: dict[str, MonitoredResourceSpecification] = {}
        self.attr_specs: dict[tuple[str, str], MonitoredAttributeSpecification] = {}
        self.active_faults: dict[str, FaultEvent] = {}
        self.active_thresholds: dict[str, ThresholdExceedanceEvent] = {}
        self.trends: dict[tuple[str, str], deque] = {}
        self.usage: dict[tuple[str, str], CharacteristicUsage] = {}
        self.event_log: deque[tuple[float, str, str]] = deque(maxlen=60)   # (시각, 종류, 내용)
        self.started_at: float | None = None
        self._cmd_writer: DataWriter | None = None

    def start(self) -> None:
        self.started_at = self.sched.now
        p = self.participant = self.domain.participant(self.name)
        q = self.qos.for_topic
        for cls, handler in (
            (MonitoredResource, self._on_resource),
            (FaultEvent, self._on_fault),
            (ThresholdExceedanceEvent, self._on_threshold),
            (CharacteristicValue, self._on_value),
            (CharacteristicUsage, self._on_usage),
            (MonitoredAttributeSpecification, self._on_attr_spec),
            (MonitoredResourceSpecification, self._on_res_spec),
        ):
            p.create_reader(cls.TOPIC, q(cls.TOPIC), handler)
        self._cmd_writer = p.create_writer(FaultClearCommand.TOPIC, q(FaultClearCommand.TOPIC))

    def stop(self) -> None:
        if self.participant is not None:
            self.participant.close()

    # -------------------------------------------------------------- 명령 (O-role)

    def clear_latched(self, resource_id: str, fault_code_id: str, operator: str = "crew") -> None:
        self._cmd_writer.write(FaultClearCommand(resource_id, fault_code_id, operator, self.sched.now))
        self._event("command", f"해제 명령 송신 {resource_id}/{fault_code_id} ({operator})")

    # -------------------------------------------------------------- 수신

    def _on_resource(self, mr: MonitoredResource | None, info: SampleInfo) -> None:
        if mr is None:
            return
        prev = self.resources.get(mr.resource_id)
        self.resources[mr.resource_id] = mr
        if prev is None or prev.health is not mr.health:
            kind = "health_red" if mr.health is Health.RED else "health_green"
            self._event(kind, f"{mr.resource_id} 건전성 {mr.health.value}")

    def _on_fault(self, ev: FaultEvent | None, info: SampleInfo) -> None:
        if ev is None:
            return
        if ev.state is EventState.ACTIVE:
            new = ev.event_id not in self.active_faults
            self.active_faults[ev.event_id] = ev
            if new:
                self._event("fault", f"고장 발생 {ev.event_id} {ev.fault_code_id} {ev.description}")
            elif ev.latched:
                self._event("latched", f"고장 래치 {ev.event_id} {ev.fault_code_id}")
        else:
            self.active_faults.pop(ev.event_id, None)
            self._event("cleared", f"고장 해제 {ev.event_id} {ev.fault_code_id}")

    def _on_threshold(self, ev: ThresholdExceedanceEvent | None, info: SampleInfo) -> None:
        if ev is None:
            return
        if ev.state is ExceedanceState.EXCEEDED:
            self.active_thresholds[ev.event_id] = ev
            self._event("exceeded",
                        f"임계 초과 {ev.event_id} {ev.resource_id}/{ev.characteristic_id} {ev.trigger_value:.1f}")
        else:
            self.active_thresholds.pop(ev.event_id, None)
            self._event("released", f"임계 복귀 {ev.event_id} {ev.resource_id}/{ev.characteristic_id}")

    def _on_value(self, cv: CharacteristicValue | None, info: SampleInfo) -> None:
        if cv is None:
            return
        key = (cv.resource_id, cv.characteristic_id)
        self.trends.setdefault(key, deque(maxlen=self.trend_len)).append((cv.timestamp, cv.value))

    def _on_usage(self, u: CharacteristicUsage | None, info: SampleInfo) -> None:
        if u is not None:
            self.usage[(u.resource_id, u.usage_id)] = u

    def _on_attr_spec(self, spec: MonitoredAttributeSpecification | None, info: SampleInfo) -> None:
        if spec is not None:
            self.attr_specs[(spec.resource_id, spec.characteristic_id)] = spec

    def _on_res_spec(self, spec: MonitoredResourceSpecification | None, info: SampleInfo) -> None:
        if spec is not None:
            self.resource_specs[spec.resource_id] = spec

    def _event(self, kind: str, text: str) -> None:
        self.event_log.append((self.sched.now, kind, text))

    def _is_stale(self, key: tuple[str, str], now: float) -> bool:
        spec = self.attr_specs.get(key)
        trend = self.trends.get(key)
        return bool(spec and trend and now - trend[-1][0] > 3.0 / spec.rate_hz)

    # -------------------------------------------------------------- 표시: 상태 사전 (웹 GUI)

    def snapshot(self) -> dict[str, Any]:
        """현재 화면 상태를 JSON 직렬화 가능한 사전으로 돌려준다."""
        now = self.sched.now
        resources = []
        for rid in sorted(set(self.resources) | set(self.resource_specs)):
            mr = self.resources.get(rid)
            spec = self.resource_specs.get(rid)
            resources.append({
                "resource_id": rid,
                "name": spec.name if spec else rid,
                "provider": spec.provider.value if spec else None,
                "rationale": spec.rationale if spec else "",
                "health": mr.health.value if mr else None,
                "pbit_complete": mr.pbit_complete if mr else None,
                "pbit_passed": mr.pbit_passed if mr else None,
                "active_faults": sum(1 for ev in self.active_faults.values() if ev.resource_id == rid),
            })
        trends = []
        for key in sorted(set(self.trends) | set(self.attr_specs)):
            spec = self.attr_specs.get(key)
            trends.append({
                "resource_id": key[0],
                "characteristic_id": key[1],
                "name": spec.name if spec else key[1],
                "unit": spec.unit if spec else "",
                "rate_hz": spec.rate_hz if spec else None,
                "max_limit": spec.max_limit if spec else None,
                "min_limit": spec.min_limit if spec else None,
                "hysteresis": spec.hysteresis if spec else None,
                "provider": spec.provider.value if spec else None,
                "points": [[round(t, 3), round(v, 3)] for t, v in self.trends.get(key, ())],
                "stale": self._is_stale(key, now),
            })
        return {
            "t": now,
            "resources": resources,
            "faults": [to_dict(ev) for ev in sorted(self.active_faults.values(), key=lambda e: e.event_id)],
            "thresholds": [to_dict(ev) for ev in sorted(self.active_thresholds.values(), key=lambda e: e.event_id)],
            "trends": trends,
            "usage": [to_dict(u) for _, u in sorted(self.usage.items())],
            "events": [{"t": t, "kind": k, "text": x} for t, k, x in reversed(self.event_log)],
        }

    # -------------------------------------------------------------- 표시: 텍스트 화면

    def render(self) -> str:
        now = self.sched.now
        lines = [f"┌─ 간이 HMI [{self.name}]  t = {now:8.2f} s " + "─" * 36]
        lines.append("│ [리소스 건전성]")
        ids = sorted(set(self.resources) | set(self.resource_specs))
        for rid in ids:
            mr = self.resources.get(rid)
            if mr is None:
                spec = self.resource_specs[rid]
                lines.append(f"│   {rid:<20} ○ N/A    (R-role 미지원, 사양 출처 {spec.provider.value})")
                continue
            mark = "●" if mr.health is Health.RED else "○"
            pbit = "진행 중" if not mr.pbit_complete else ("통과" if mr.pbit_passed else "실패")
            lines.append(f"│   {rid:<20} {mark} {mr.health.value:<6} PBIT {pbit}")
        lines.append("│ [활성 고장]")
        if not self.active_faults:
            lines.append("│   (없음)")
        for ev in sorted(self.active_faults.values(), key=lambda e: e.event_id):
            latch = " [래치]" if ev.latched else ""
            lines.append(
                f"│   {ev.event_id} {ev.resource_id}/{ev.fault_code_id} {ev.description} "
                f"({ev.severity}, {ev.bit_type.value}){latch}  발생 t={ev.raised_time:.2f}s"
            )
        lines.append("│ [임계값 초과]")
        if not self.active_thresholds:
            lines.append("│   (없음)")
        for ev in sorted(self.active_thresholds.values(), key=lambda e: e.event_id):
            lines.append(
                f"│   {ev.event_id} {ev.resource_id}/{ev.characteristic_id} {ev.limit_type.value} "
                f"{ev.limit_value:g} (값 {ev.trigger_value:.1f}, 사양 {ev.spec_provider.value})"
            )
        lines.append(f"│ [특성값 추이 (최근 {self.trend_len}개)]")
        for key in sorted(self.trends):
            trend = self.trends[key]
            spec = self.attr_specs.get(key)
            _, v_last = trend[-1]
            unit = spec.unit if spec else ""
            name = spec.name if spec else key[1]
            stale = "  (갱신 지연)" if self._is_stale(key, now) else ""
            spark = sparkline([v for _, v in trend])
            label = f"{key[0]}/{key[1]}"
            lines.append(f"│   {label:<30} {name:<18} {v_last:8.1f} {unit:<5} {spark}{stale}")
        lines.append("│ [최근 이벤트]")
        for t, _, text in list(self.event_log)[-8:]:
            lines.append(f"│   t={t:8.2f}s  {text}")
        lines.append("└" + "─" * 70)
        return "\n".join(lines)
