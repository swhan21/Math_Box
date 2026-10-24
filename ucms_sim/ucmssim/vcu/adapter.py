"""UCM 어댑터 (VCU 측 R-role 구현, 설계안 6.3절).

| 모듈                  | 입력                 | 출력 토픽                                   |
|-----------------------|----------------------|---------------------------------------------|
| FaultMapper           | 내부 DTC             | Fault_Code, Fault_Code_Specification        |
| CharacteristicSampler | 제어 SW 내부 변수    | Monitored_Characteristic__supply...Value    |
| HealthEvaluator       | 활성 고장, PBIT 결과 | Resource_With_Monitoring                    |
| UsageCounter          | 전원·이벤트 신호     | Characteristic_Usage                        |
| PbitReporter          | 기동 시 자체점검     | Resource_With_Monitoring (PBIT 필드)        |
| DdsPublisher          | 위 모듈 출력         | —                                           |

DdsPublisher 외의 모듈은 DDS를 모르므로 단독으로 단위시험할 수 있다 (6.1절 '시험 용이성').
어댑터는 제어 주기와 분리된 저우선 태스크에서 돌고, 예외는 제어 SW로 전파되지 않는다.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..config import CharacteristicDef, FaultDef, HealthRule, UsageDef, VcuUcmConfig
from ..config import QosConfig
from ..dds import Participant, SampleInfo
from ..sched import Scheduler, Timer
from ..topics import (
    CharacteristicUsage,
    CharacteristicValue,
    ClearPolicy,
    FaultClearCommand,
    FaultCode,
    FaultCodeSpecification,
    FaultState,
    Health,
    MonitoredAttributeSpecification,
    MonitoredResourceSpecification,
    ResourceRegistration,
    ResourceWithMonitoring,
    SpecProvider,
)
from .channel import DiagnosticChannel, DtcRecord, EventRecord, PbitRecord
from .nvm import NvmStore

ACTIVE_STATES = (FaultState.ACTIVE, FaultState.LATCHED)


class FaultMapper:
    """내부 DTC → UCM 고장코드 매핑과 상태 전이 (6.5절 고장코드 상태도).

    Inactive → Active        : BIT 검출
    Active   → Inactive      : 조건 해소 (auto)
    Active   → Latched       : 조건 해소 (latch)
    Active   (유지)           : 조건 해소 (restart — 재기동 시에만 해제)
    Latched  → Inactive      : 운용자 해제 (또는 재기동: 새 매퍼는 Inactive에서 시작)
    """

    def __init__(self, faults: Sequence[FaultDef]) -> None:
        self.defs = {f.id: f for f in faults}
        self._by_dtc = {f.internal_dtc: f for f in faults}
        self.states = {f.id: FaultState.INACTIVE for f in faults}
        self._condition = {f.id: False for f in faults}
        self.unknown_dtcs: set[int] = set()

    def on_dtc(self, dtc: int, active: bool) -> str | None:
        """상태가 바뀐 고장코드 ID를 돌려준다 (변화 없으면 None)."""
        f = self._by_dtc.get(dtc)
        if f is None:
            self.unknown_dtcs.add(dtc)
            return None
        self._condition[f.id] = active
        old = self.states[f.id]
        if active:
            new = FaultState.ACTIVE
        elif old is FaultState.ACTIVE:
            new = {
                ClearPolicy.AUTO: FaultState.INACTIVE,
                ClearPolicy.LATCH: FaultState.LATCHED,
                ClearPolicy.RESTART: FaultState.ACTIVE,
            }[f.clear]
        else:
            new = old
        if new is old:
            return None
        self.states[f.id] = new
        return f.id

    def operator_clear(self, fault_id: str) -> bool:
        """래치 고장의 운용자 해제. 조건이 남아 있거나 래치 상태가 아니면 거부."""
        if self.states.get(fault_id) is FaultState.LATCHED and not self._condition[fault_id]:
            self.states[fault_id] = FaultState.INACTIVE
            return True
        return False

    def active_count(self) -> int:
        return sum(1 for s in self.states.values() if s in ACTIVE_STATES)


class HealthEvaluator:
    """건전성 규칙 적용 (7.4절). 래치 고장도 해제 전까지는 활성으로 본다."""

    def __init__(self, rule: HealthRule, defs: Mapping[str, FaultDef]) -> None:
        self.rule = rule
        self.defs = defs

    def evaluate(self, pbit_complete: bool, pbit_passed: bool, states: Mapping[str, FaultState]) -> Health:
        for cond in self.rule.red_if:
            if cond.kind == "pbit_failed" and pbit_complete and not pbit_passed:
                return Health.RED
            if cond.kind == "any_active_fault_with_severity" and any(
                s in ACTIVE_STATES and self.defs[fid].severity is cond.severity for fid, s in states.items()
            ):
                return Health.RED
        return self.rule.otherwise


class CharacteristicSampler:
    """설정 주기마다 공유 메모리 스냅샷에서 특성값을 읽는다. 무효값(None)은 발행하지 않는다."""

    def __init__(self, chars: Sequence[CharacteristicDef], t_start: float) -> None:
        self.chars = chars
        self._next = {c.id: t_start for c in chars}
        self.invalid_skips = {c.id: 0 for c in chars}

    def due(self, t: float, signals: Mapping[str, Any]) -> list[tuple[CharacteristicDef, float]]:
        out = []
        for c in self.chars:
            nxt = self._next[c.id]
            if t + 1e-9 < nxt:
                continue
            period = 1.0 / c.rate_hz
            self._next[c.id] = nxt + (max(0, math.floor((t - nxt) / period + 1e-9)) + 1) * period
            v = signals.get(c.signal)
            if v is None:
                self.invalid_skips[c.id] += 1
                continue
            out.append((c, float(v)))
        return out


class UsageCounter:
    """가동시간·비상정지 횟수 누적과 비휘발 저장 (7.3절)."""

    BLOCK = "usage"
    EVENT_TYPES = {"emergency_stop": "emergency_stop_count"}

    def __init__(self, defs: Sequence[UsageDef], nvm: NvmStore, log: Callable[[str], None]) -> None:
        self.defs = {d.id: d for d in defs}
        self.nvm = nvm
        data, ok = nvm.read(self.BLOCK)
        self.load_ok = ok
        if not ok:
            log("사용량 블록 CRC 불일치 → 0으로 초기화")
        data = data if ok and data else {}
        self.values = {d.id: float(data.get(d.id, 0.0)) for d in defs}
        self._next_pub: dict[str, float] = {}
        self._dirty: set[str] = set(self.defs)        # 기동 시 현재값 1회 발행

    def tick(self, dt: float) -> None:
        for d in self.defs.values():
            if d.type == "powered_on_time":
                self.values[d.id] += dt

    def on_event(self, name: str) -> None:
        usage_type = self.EVENT_TYPES.get(name)
        for d in self.defs.values():
            if d.type == usage_type:
                self.values[d.id] += 1
                self._dirty.add(d.id)

    def due(self, t: float) -> list[str]:
        out = []
        for d in self.defs.values():
            periodic = d.publish_period_s is not None and t + 1e-9 >= self._next_pub.get(d.id, t)
            if d.id in self._dirty or periodic:
                out.append(d.id)
                if d.publish_period_s is not None:
                    self._next_pub[d.id] = t + d.publish_period_s
        self._dirty.clear()
        return out

    def persist(self) -> None:
        self.nvm.write(self.BLOCK, {d.id: self.values[d.id] for d in self.defs.values() if d.persist})


class PbitReporter:
    """기동 시 자체점검 결과를 1회 보고용으로 보관한다."""

    def __init__(self) -> None:
        self.complete = False
        self.passed = False

    def on_result(self, passed: bool) -> None:
        self.complete = True
        self.passed = passed


class DdsPublisher:
    """R-role 발행 토픽의 DataWriter 묶음. QoS는 qos.yaml 로 토픽별 적용."""

    TOPICS = (
        "Resource_Registration",
        "Resource_With_Monitoring",
        "Monitored_Resource_Specification",
        "Monitored_Attribute_Specification",
        "Fault_Code_Specification",
        "Fault_Code",
        "Monitored_Characteristic__supplyCharacteristicValue",
        "Characteristic_Usage",
    )

    def __init__(self, participant: Participant, qos: QosConfig) -> None:
        self._writers = {t: participant.create_writer(t, qos.for_topic(t)) for t in self.TOPICS}

    def publish(self, sample: Any) -> None:
        self._writers[sample.TOPIC].write(sample)

    def publish_all(self, samples: Iterable[Any]) -> None:
        for s in samples:
            self.publish(s)


class UcmAdapter:
    def __init__(
        self,
        cfg: VcuUcmConfig,
        channel: DiagnosticChannel,
        participant: Participant,
        qos: QosConfig,
        sched: Scheduler,
        nvm: NvmStore,
        boot_count: int,
    ) -> None:
        self.cfg = cfg
        self.channel = channel
        self.participant = participant
        self.qos = qos
        self.sched = sched
        self.boot_count = boot_count
        self.log: list[tuple[float, str]] = []
        self.errors = 0
        self.publisher = DdsPublisher(participant, qos)
        self.mapper = FaultMapper(cfg.faults)
        self.health = HealthEvaluator(cfg.health_rule, self.mapper.defs)
        self.pbit = PbitReporter()
        self.usage = UsageCounter(cfg.usage, nvm, self._log)
        self.sampler: CharacteristicSampler | None = None
        self._commands: deque[FaultClearCommand] = deque()
        self._last_health: tuple | None = None
        self._timer: Timer | None = None
        self._last_tick = 0.0
        self._next_persist = 0.0

    # -------------------------------------------------------------- 기동·정지

    def start(self) -> None:
        cfg, t = self.cfg, self.sched.now
        rid = cfg.resource_id
        pub = self.publisher
        pub.publish(ResourceRegistration(rid, cfg.name, cfg.kind, self.boot_count, t))
        pub.publish(MonitoredResourceSpecification(
            rid, cfg.name, "UGV 차량제어기", SpecProvider.RESOURCE, "R-role 자체 발행"
        ))
        pub.publish_all(self._attribute_specs())
        pub.publish_all(
            FaultCodeSpecification(rid, f.id, f.internal_dtc, f.description, f.bit_type, f.severity, f.clear)
            for f in cfg.faults
        )
        # 현재 상태 전체 발행: 재기동 후 UCMS가 이전 고장 상태와 정합을 맞출 수 있게 한다
        pub.publish_all(FaultCode(rid, f.id, FaultState.INACTIVE, f.bit_type, t) for f in cfg.faults)
        self._publish_health(t)
        self.participant.create_reader(
            "Fault_Code__clearFault", self.qos.for_topic("Fault_Code__clearFault"), self._on_clear_command
        )
        period = cfg.task_period_s
        self.sampler = CharacteristicSampler(cfg.characteristics, t_start=t + period)
        self._last_tick = t
        self._next_persist = t + 10.0
        self._timer = self.sched.every(period, self._task, start=t + period)

    def stop(self, persist: bool = True) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if persist:
            self.usage.persist()

    def _attribute_specs(self) -> list[MonitoredAttributeSpecification]:
        out = []
        for c in self.cfg.characteristics:
            th = c.threshold
            out.append(MonitoredAttributeSpecification(
                resource_id=self.cfg.resource_id,
                characteristic_id=c.id,
                name=c.name,
                unit=c.unit,
                rate_hz=c.rate_hz,
                max_limit=th.max if th else None,
                min_limit=th.min if th else None,
                hysteresis=th.hysteresis if th else 0.0,
                attack_samples=th.attack_samples if th else 1,
                release_samples=th.release_samples if th else 1,
                provider=SpecProvider.RESOURCE,
                rationale="ICD 7.2절 (엔지니어링 물리 한계값)",
            ))
        return out

    # -------------------------------------------------------------- 저우선 태스크

    def _on_clear_command(self, cmd: FaultClearCommand | None, info: SampleInfo) -> None:
        if cmd is not None and cmd.resource_id == self.cfg.resource_id:
            self._commands.append(cmd)          # 다음 태스크 주기에 처리

    def _task(self) -> None:
        try:
            self._step()
        except Exception as e:                  # 어댑터 장애는 제어 SW로 전파하지 않는다
            self.errors += 1
            self._log(f"어댑터 예외: {e!r}")

    def _step(self) -> None:
        t = self.sched.now
        rid = self.cfg.resource_id
        pub = self.publisher

        for rec in self.channel.drain():
            if isinstance(rec, DtcRecord):
                fid = self.mapper.on_dtc(rec.dtc, rec.active)
                if fid is not None:
                    pub.publish(FaultCode(rid, fid, self.mapper.states[fid], self.mapper.defs[fid].bit_type, rec.t))
            elif isinstance(rec, PbitRecord):
                self.pbit.on_result(rec.passed)
            elif isinstance(rec, EventRecord):
                self.usage.on_event(rec.name)

        while self._commands:
            cmd = self._commands.popleft()
            if self.mapper.operator_clear(cmd.fault_code_id):
                fid = cmd.fault_code_id
                pub.publish(FaultCode(rid, fid, FaultState.INACTIVE, self.mapper.defs[fid].bit_type, t))
                self._log(f"{fid} 운용자 해제 ({cmd.operator})")
            else:
                self._log(f"{cmd.fault_code_id} 해제 거부 (래치 상태 아님 또는 조건 지속)")

        self._publish_health(t)

        _, signals = self.channel.snapshot()
        for c, v in self.sampler.due(t, signals):
            pub.publish(CharacteristicValue(rid, c.id, v, t))

        self.usage.tick(t - self._last_tick)
        self._last_tick = t
        for uid in self.usage.due(t):
            d = self.usage.defs[uid]
            pub.publish(CharacteristicUsage(rid, uid, d.type, self.usage.values[uid], d.unit, t))
        if t + 1e-9 >= self._next_persist:
            self.usage.persist()
            self._next_persist += 10.0

    def _publish_health(self, t: float) -> None:
        health = self.health.evaluate(self.pbit.complete, self.pbit.passed, self.mapper.states)
        state = (health, self.pbit.complete, self.pbit.passed, self.mapper.active_count())
        if state != self._last_health:
            self._last_health = state
            self.publisher.publish(ResourceWithMonitoring(self.cfg.resource_id, *state, t))

    def _log(self, msg: str) -> None:
        self.log.append((self.sched.now, msg))
