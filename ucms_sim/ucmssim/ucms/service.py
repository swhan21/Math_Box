"""최소 UCMS 서비스: DDS I/O + 모듈 연결 (설계안 6.2절 컴포넌트도의 SVC).

    구독                                   → 모듈                → 발행
    Resource_Registration                  → (등록 기록)
    Resource_With_Monitoring               → HealthRepublisher    → Monitored_Resource
    *_Specification                        → SpecRegistry         → (브리지 사양)
    Fault_Code                             → FaultEventDeriver    → Fault_Event
    supplyCharacteristicValue              → ThresholdEvaluator   → Threshold_Exceedance_Event
    Characteristic_Usage                   → UsageTracker
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import BridgeConfig, QosConfig, UcmsSettings
from ..dds import DataWriter, Domain, Participant, SampleInfo
from ..deviation import DEVIATIONS
from ..sched import Scheduler, Timer
from ..topics import (
    CharacteristicUsage,
    CharacteristicValue,
    FaultCode,
    FaultCodeSpecification,
    MonitoredAttributeSpecification,
    MonitoredResourceSpecification,
    ResourceRegistration,
    ResourceWithMonitoring,
    key_of,
)
from .fault_events import FaultEventDeriver
from .health_republisher import HealthRepublisher
from .persistence import UcmsStore
from .spec_registry import SpecRegistry
from .threshold import ThresholdEvaluator
from .usage_tracker import UsageTracker

PUBLISHED_TOPICS = (
    "Monitored_Resource",
    "Fault_Event",
    "Threshold_Exceedance_Event",
    "Monitored_Resource_Specification",
    "Monitored_Attribute_Specification",
)


class UcmsService:
    def __init__(
        self,
        sched: Scheduler,
        domain: Domain,
        qos: QosConfig,
        bridge: BridgeConfig,
        settings: UcmsSettings,
        db_path: str,
        name: str = "UCMS",
    ) -> None:
        self.sched = sched
        self.domain = domain
        self.qos = qos
        self.bridge = bridge
        self.settings = settings
        self.db_path = db_path
        self.name = name
        self.running = False
        self.samples_received = 0
        self.participant: Participant | None = None
        self.store: UcmsStore | None = None
        self._writers: dict[str, DataWriter] = {}
        self._timers: list[Timer] = []

    # -------------------------------------------------------------- 기동·정지

    def start(self) -> None:
        now = self.sched.now
        self.store = UcmsStore(self.db_path)
        self.store.write_deviations(DEVIATIONS)
        self._log("INFO", f"{self.name} 기동")

        p = self.participant = self.domain.participant(self.name)
        self._writers = {t: p.create_writer(t, self.qos.for_topic(t)) for t in PUBLISHED_TOPICS}

        clock = lambda: self.sched.now  # noqa: E731
        self.registry = SpecRegistry(self.bridge, clock, self.store, self._publish)
        self.health = HealthRepublisher(self.store, self._publish, clock)
        self.faults = FaultEventDeriver(
            self.store, self.registry, self._publish, self._dispose, clock, self._call_later,
            self.settings.fault_spec_wait_s, self._log,
        )
        self.thresholds = ThresholdEvaluator(
            self.store, self.registry, self._publish, self._dispose, clock, self._log
        )
        self.usage = UsageTracker(self.store, clock)
        self.faults.restore()
        self.thresholds.restore()

        subscriptions: list[tuple[str, Callable[[Any], None]]] = [
            (ResourceRegistration.TOPIC, lambda s: self.store.upsert_resource(self.sched.now, s)),
            (ResourceWithMonitoring.TOPIC, self.health.on_resource_with_monitoring),
            (MonitoredResourceSpecification.TOPIC, self.registry.on_resource_spec),
            (MonitoredAttributeSpecification.TOPIC, self.registry.on_attribute_spec),
            (FaultCodeSpecification.TOPIC, self._on_fault_spec),
            (FaultCode.TOPIC, self.faults.on_fault_code),
            (CharacteristicValue.TOPIC, self._on_value),
            (CharacteristicUsage.TOPIC, self.usage.on_usage),
        ]
        for topic, handler in subscriptions:
            p.create_reader(topic, self.qos.for_topic(topic), self._wrap(handler), ignore_local=True)

        period = self.settings.sample_commit_period_s
        self._timers = [
            self.sched.call_later(self.bridge.grace_s, self.registry.expire_grace),
            self.sched.every(period, self.store.flush, start=now + period),
        ]
        self.running = True

    def stop(self) -> None:
        """정지 (재기동 시험용). DB는 닫고 DDS 참여자는 종료한다."""
        if not self.running:
            return
        self.running = False
        for timer in self._timers:
            timer.cancel()
        self._log("INFO", f"{self.name} 정지")
        self.participant.close()
        self.store.close()

    # -------------------------------------------------------------- 내부

    def _wrap(self, handler: Callable[[Any], None]) -> Callable[[Any, SampleInfo], None]:
        """수신 → 처리 지연 후 처리. dispose 통지는 무시한다."""
        delay = self.settings.processing_delay_s

        def on_data(sample: Any, info: SampleInfo) -> None:
            if sample is None or not self.running:
                return
            if delay > 0:
                self.sched.call_later(delay, self._guarded, handler, sample)
            else:
                handler(sample)

        return on_data

    def _guarded(self, fn: Callable, *args: Any) -> None:
        if self.running:
            fn(*args)

    def _call_later(self, delay: float, fn: Callable, *args: Any) -> None:
        """정지 후에는 실행되지 않는 지연 호출 (모듈 내부 타이머용)."""
        self.sched.call_later(delay, self._guarded, fn, *args)

    def _on_fault_spec(self, spec: FaultCodeSpecification) -> None:
        self.registry.on_fault_spec(spec)
        self.faults.on_fault_spec(spec)

    def _on_value(self, cv: CharacteristicValue) -> None:
        self.samples_received += 1
        self.store.add_sample(self.sched.now, cv)
        self.thresholds.on_value(cv)

    def _publish(self, sample: Any) -> None:
        self._writers[sample.TOPIC].write(sample)

    def _dispose(self, sample: Any) -> None:
        self._writers[sample.TOPIC].dispose(key_of(sample))

    def _log(self, level: str, message: str) -> None:
        self.store.log(self.sched.now, level, message)
