"""SIL 시험대: 설계안 5.1절(방식 A) 구성도를 한 프로세스에 구성한다.

    시험 하니스 ─(고장 주입)→ 플랜트 모델 ─(CAN·센서)→ VCU(기존 제어·BIT + UCM 어댑터)
    단순 장비 ─DDS(값만)─┐                                │DDS
                          └──────────→ 최소 UCMS ←─────────┘
                                       │DDS
                              간이 HMI · 하니스 구독자(t1 기록)
"""

from __future__ import annotations

import os
import random
from typing import Callable

from ..config import Configs
from ..dds import Domain, SampleInfo
from ..hmi import SimpleHmi
from ..plant import UgvPlant
from ..sched import Scheduler, Timer
from ..simple_device import SimpleTempSensor
from ..topics import (
    CharacteristicValue,
    FaultEvent,
    Health,
    MonitoredResource,
    ThresholdExceedanceEvent,
    fault_event_tag,
    threshold_event_tag,
)
from ..ucms.persistence import read_event_ledger
from ..ucms.service import UcmsService
from ..vcu.control import DEFAULT_CALIBRATION
from ..vcu.node import VcuNode
from ..vcu.nvm import NvmStore


class HarnessObserver:
    """시험 하니스의 DDS 구독자. 수신 시각(t1)을 하니스 시계로 기록한다 (5.4절)."""

    def __init__(self, sched: Scheduler, domain: Domain, qos) -> None:
        self.sched = sched
        self.domain = domain
        self.qos = qos
        self.fault_events: list[tuple[float, FaultEvent]] = []
        self.threshold_events: list[tuple[float, ThresholdExceedanceEvent]] = []
        self.health: list[tuple[float, MonitoredResource]] = []
        self.values: dict[tuple[str, str], list[tuple[float, float]]] = {}
        self.ledger: set[tuple[str, str, str]] = set()

    def start(self) -> None:
        p = self.domain.participant("HARNESS")
        q = self.qos.for_topic
        p.create_reader(FaultEvent.TOPIC, q(FaultEvent.TOPIC), self._on_fault)
        p.create_reader(ThresholdExceedanceEvent.TOPIC, q(ThresholdExceedanceEvent.TOPIC), self._on_threshold)
        p.create_reader(MonitoredResource.TOPIC, q(MonitoredResource.TOPIC), self._on_health)
        p.create_reader(CharacteristicValue.TOPIC, q(CharacteristicValue.TOPIC), self._on_value)

    def _on_fault(self, ev: FaultEvent | None, info: SampleInfo) -> None:
        if ev is not None:
            self.fault_events.append((self.sched.now, ev))
            self.ledger.add((FaultEvent.TOPIC, ev.event_id, fault_event_tag(ev)))

    def _on_threshold(self, ev: ThresholdExceedanceEvent | None, info: SampleInfo) -> None:
        if ev is not None:
            self.threshold_events.append((self.sched.now, ev))
            self.ledger.add((ThresholdExceedanceEvent.TOPIC, ev.event_id, threshold_event_tag(ev)))

    def _on_health(self, mr: MonitoredResource | None, info: SampleInfo) -> None:
        if mr is not None:
            self.health.append((self.sched.now, mr))

    def _on_value(self, cv: CharacteristicValue | None, info: SampleInfo) -> None:
        if cv is not None:
            self.values.setdefault((cv.resource_id, cv.characteristic_id), []).append((self.sched.now, cv.value))

    # -------------------------------------------------------------- 조회

    def find_fault(self, fault_code_id: str, tag: str, after: float) -> tuple[float, FaultEvent] | None:
        for t, ev in self.fault_events:
            if t >= after and ev.fault_code_id == fault_code_id and fault_event_tag(ev) == tag:
                return t, ev
        return None

    def find_threshold(self, characteristic_id: str, tag: str, after: float):
        for t, ev in self.threshold_events:
            if t >= after and ev.characteristic_id == characteristic_id and threshold_event_tag(ev) == tag:
                return t, ev
        return None

    def find_health(
        self, resource_id: str, after: float, pred: Callable[[MonitoredResource], bool]
    ) -> tuple[float, MonitoredResource] | None:
        for t, mr in self.health:
            if t >= after and mr.resource_id == resource_id and pred(mr):
                return t, mr
        return None

    def latest_health(self, resource_id: str) -> MonitoredResource | None:
        for _, mr in reversed(self.health):
            if mr.resource_id == resource_id:
                return mr
        return None


class Testbed:
    def __init__(
        self,
        cfg: Configs,
        seed: int,
        db_path: str,
        *,
        adapter_enabled: bool = True,
        ucm_calls: bool = True,
        profile_control: bool = False,
        realtime: float | None = None,
        device_spec=None,
        device_spec_delay_s: float = 0.0,
    ) -> None:
        self.cfg = cfg
        self.seed = seed
        self.db_path = db_path
        if os.path.exists(db_path):
            os.remove(db_path)
        sim = cfg.sim
        self.sched = Scheduler(realtime)
        self.rng = random.Random(seed)                      # 하니스 전용 (주입 위상 등)
        self.domain = Domain(self.sched, latency=sim.latency, rng=random.Random(seed + 1))
        self.plant = UgvPlant(sim.plant, rng=random.Random(seed + 2))
        self.nvm = NvmStore()
        self.nvm.write("calibration", DEFAULT_CALIBRATION)  # 공장 출하 상태
        self.vcu = VcuNode(
            self.sched, self.domain, self.plant, self.nvm, cfg.vcu, sim, cfg.qos,
            adapter_enabled=adapter_enabled, ucm_calls=ucm_calls, profile=profile_control,
        )
        self.device = SimpleTempSensor(
            self.sched, self.domain, self.plant, cfg.qos, random.Random(seed + 3),
            spec=device_spec, spec_delay_s=device_spec_delay_s,
        )
        self.ucms = self._new_ucms()
        self.hmi = SimpleHmi(self.sched, self.domain, cfg.qos)
        self.observer = HarnessObserver(self.sched, self.domain, cfg.qos)
        self._timers: list[Timer] = []
        self.closed = False

    def _new_ucms(self) -> UcmsService:
        return UcmsService(self.sched, self.domain, self.cfg.qos, self.cfg.bridge, self.cfg.sim.ucms, self.db_path)

    @property
    def now(self) -> float:
        return self.sched.now

    @property
    def vcu_id(self) -> str:
        return self.cfg.vcu.resource_id

    # -------------------------------------------------------------- 실행 제어

    def start(self) -> None:
        dt = self.cfg.sim.plant_step_s
        self._timers.append(self.sched.every(dt, lambda: self.plant.step(self.sched.now, dt)))
        self.observer.start()
        self.ucms.start()
        self.hmi.start()
        self.device.start()
        self.vcu.power_on()

    def bring_up(self, speed_kph: float = 20.0, settle_s: float | None = None, timeout: float = 15.0) -> bool:
        """기동 → VCU PBIT 완료·브리지 대기 경과 확인 → 주행 안정화."""
        self.start()
        ok = self.run_until(
            lambda: self.ucms.registry.grace_expired and self._vcu_ready(), timeout
        )
        self.vcu.set_speed(speed_kph)
        self.run_for(self.cfg.sim.harness.settle_s if settle_s is None else settle_s)
        return ok

    def _vcu_ready(self) -> bool:
        mr = self.observer.latest_health(self.vcu_id)
        return mr is not None and mr.pbit_complete

    def run_for(self, duration: float) -> None:
        self.sched.run_for(duration)

    def run_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        return self.sched.run_until_true(predicate, timeout)

    def every(self, period: float, fn: Callable[[], None]) -> Timer:
        timer = self.sched.every(period, fn)
        self._timers.append(timer)
        return timer

    def stop_ucms(self) -> None:
        self.ucms.stop()

    def start_new_ucms(self) -> None:
        self.ucms = self._new_ucms()
        self.ucms.start()

    # -------------------------------------------------------------- 대기 도우미 (결과 반환)

    def wait_fault(self, fault_code_id: str, tag: str, after: float, timeout: float):
        obs = self.observer
        self.run_until(lambda: obs.find_fault(fault_code_id, tag, after) is not None, timeout)
        return obs.find_fault(fault_code_id, tag, after)

    def wait_threshold(self, characteristic_id: str, tag: str, after: float, timeout: float):
        obs = self.observer
        self.run_until(lambda: obs.find_threshold(characteristic_id, tag, after) is not None, timeout)
        return obs.find_threshold(characteristic_id, tag, after)

    def wait_health(self, after: float, timeout: float, pred: Callable[[MonitoredResource], bool], resource_id=None):
        rid = resource_id or self.vcu_id
        obs = self.observer
        self.run_until(lambda: obs.find_health(rid, after, pred) is not None, timeout)
        return obs.find_health(rid, after, pred)

    def wait_health_is(self, health: Health, after: float, timeout: float):
        return self.wait_health(after, timeout, lambda mr: mr.health is health)

    # -------------------------------------------------------------- 종료·검증

    def close(self) -> None:
        if self.closed:
            return
        self.run_for(0.3)                                   # 전송 중 샘플 수신 대기
        for timer in self._timers:
            timer.cancel()
        self.ucms.stop()
        self.closed = True

    def ledger_diff(self) -> tuple[set, set]:
        """(DB 누락, 하니스 미수신) — SC5: 발행되어 수신된 이벤트 전이가 모두 DB에 있는가."""
        db = read_event_ledger(self.db_path)
        return self.observer.ledger - db, db - self.observer.ledger
