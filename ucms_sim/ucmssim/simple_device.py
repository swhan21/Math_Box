"""단순 장비 1종 (브리지 검증용, RQ3).

R-role을 구현하지 못하는 온도센서: 값만 발행하고 사양·건전성은 발행하지 않는다.
시험용으로 자체 사양을 발행하게 할 수도 있다 (spec, spec_delay_s) — '리소스 발행 우선' 검증용.
"""

from __future__ import annotations

import random

from .config import QosConfig
from .dds import Domain, Participant
from .plant import UgvPlant
from .sched import Scheduler, Timer
from .topics import CharacteristicValue, MonitoredAttributeSpecification


class SimpleTempSensor:
    def __init__(
        self,
        sched: Scheduler,
        domain: Domain,
        plant: UgvPlant,
        qos: QosConfig,
        rng: random.Random,
        resource_id: str = "SIMPLE_TEMP_SENSOR",
        characteristic_id: str = "cabin_temp",
        period_s: float = 1.0,
        spec: MonitoredAttributeSpecification | None = None,
        spec_delay_s: float = 0.0,
    ) -> None:
        self.sched = sched
        self.domain = domain
        self.plant = plant
        self.qos = qos
        self.rng = rng
        self.resource_id = resource_id
        self.characteristic_id = characteristic_id
        self.period_s = period_s
        self.spec = spec
        self.spec_delay_s = spec_delay_s
        self.participant: Participant | None = None
        self._timers: list[Timer] = []

    def start(self) -> None:
        self.participant = self.domain.participant(self.resource_id)
        topic = CharacteristicValue.TOPIC
        self._writer = self.participant.create_writer(topic, self.qos.for_topic(topic))
        if self.spec is not None:
            self._timers.append(self.sched.call_later(self.spec_delay_s, self._publish_spec))
        self._timers.append(self.sched.every(self.period_s, self._publish, start=self.sched.now + self.period_s))

    def stop(self) -> None:
        for timer in self._timers:
            timer.cancel()
        if self.participant is not None:
            self.participant.close()

    def _publish(self) -> None:
        value = self.plant.cabin_temp + self.rng.gauss(0.0, 0.1)
        self._writer.write(CharacteristicValue(self.resource_id, self.characteristic_id, value, self.sched.now))

    def _publish_spec(self) -> None:
        topic = MonitoredAttributeSpecification.TOPIC
        self.participant.create_writer(topic, self.qos.for_topic(topic)).write(self.spec)
