"""간이 HMI (O-role 일부): 리소스 건전성, 활성 고장, 임계값 이벤트, 특성값 추이 표시.

UCMS가 발행한 토픽만 구독한다. 상태 토픽이 TRANSIENT_LOCAL 이므로 늦게 접속해도
현재 상태를 즉시 표시한다 (FI-09). 래치 고장 운용자 해제 명령을 보낼 수 있다.
"""

from __future__ import annotations

from collections import deque

from .config import QosConfig
from .dds import DataWriter, Domain, Participant, SampleInfo
from .sched import Scheduler
from .topics import (
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
        self.event_log: deque[str] = deque(maxlen=8)
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
        self._event(f"해제 명령 송신 {resource_id}/{fault_code_id} ({operator})")

    # -------------------------------------------------------------- 수신

    def _on_resource(self, mr: MonitoredResource | None, info: SampleInfo) -> None:
        if mr is None:
            return
        prev = self.resources.get(mr.resource_id)
        self.resources[mr.resource_id] = mr
        if prev is None or prev.health is not mr.health:
            self._event(f"{mr.resource_id} 건전성 {mr.health.value}")

    def _on_fault(self, ev: FaultEvent | None, info: SampleInfo) -> None:
        if ev is None:
            return
        if ev.state is EventState.ACTIVE:
            new = ev.event_id not in self.active_faults
            self.active_faults[ev.event_id] = ev
            if new:
                self._event(f"고장 발생 {ev.event_id} {ev.fault_code_id} {ev.description}")
            elif ev.latched:
                self._event(f"고장 래치 {ev.event_id} {ev.fault_code_id}")
        else:
            self.active_faults.pop(ev.event_id, None)
            self._event(f"고장 해제 {ev.event_id} {ev.fault_code_id}")

    def _on_threshold(self, ev: ThresholdExceedanceEvent | None, info: SampleInfo) -> None:
        if ev is None:
            return
        if ev.state is ExceedanceState.EXCEEDED:
            self.active_thresholds[ev.event_id] = ev
            self._event(f"임계 초과 {ev.event_id} {ev.resource_id}/{ev.characteristic_id} {ev.trigger_value:.1f}")
        else:
            self.active_thresholds.pop(ev.event_id, None)
            self._event(f"임계 복귀 {ev.event_id} {ev.resource_id}/{ev.characteristic_id}")

    def _on_value(self, cv: CharacteristicValue | None, info: SampleInfo) -> None:
        if cv is None:
            return
        key = (cv.resource_id, cv.characteristic_id)
        self.trends.setdefault(key, deque(maxlen=self.trend_len)).append((cv.timestamp, cv.value))

    def _on_attr_spec(self, spec: MonitoredAttributeSpecification | None, info: SampleInfo) -> None:
        if spec is not None:
            self.attr_specs[(spec.resource_id, spec.characteristic_id)] = spec

    def _on_res_spec(self, spec: MonitoredResourceSpecification | None, info: SampleInfo) -> None:
        if spec is not None:
            self.resource_specs[spec.resource_id] = spec

    def _event(self, text: str) -> None:
        self.event_log.append(f"t={self.sched.now:8.2f}s  {text}")

    # -------------------------------------------------------------- 표시

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
            t_last, v_last = trend[-1]
            unit = spec.unit if spec else ""
            name = spec.name if spec else key[1]
            stale = "  (갱신 지연)" if spec and now - t_last > 3.0 / spec.rate_hz else ""
            spark = sparkline([v for _, v in trend])
            label = f"{key[0]}/{key[1]}"
            lines.append(f"│   {label:<30} {name:<18} {v_last:8.1f} {unit:<5} {spark}{stale}")
        lines.append("│ [최근 이벤트]")
        for text in self.event_log:
            lines.append(f"│   {text}")
        lines.append("└" + "─" * 70)
        return "\n".join(lines)
