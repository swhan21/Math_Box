"""T1 단위시험: UCMS 모듈 (Spec Registry·Fault Event Deriver·저장소)."""

import os
import tempfile
import unittest

from ucmssim.config import load_configs
from ucmssim.sched import Scheduler
from ucmssim.topics import (
    BitType,
    ClearPolicy,
    EventState,
    FaultCode,
    FaultCodeSpecification,
    FaultState,
    MonitoredAttributeSpecification,
    Severity,
    SpecProvider,
)
from ucmssim.ucms.fault_events import FaultEventDeriver
from ucmssim.ucms.persistence import UcmsStore, read_event_ledger
from ucmssim.ucms.spec_registry import SpecRegistry


def resource_spec(max_limit=45.0):
    return MonitoredAttributeSpecification(
        "SIMPLE_TEMP_SENSOR", "cabin_temp", "cabin_temp", "degC", 1.0, max_limit, None, 1.0, 3, 3,
        SpecProvider.RESOURCE, "자체 발행",
    )


class UcmsUnitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "u.sqlite")
        self.store = UcmsStore(self.db)
        self.sched = Scheduler()
        self.published = []
        self.cfg = load_configs()
        self.registry = SpecRegistry(self.cfg.bridge, lambda: self.sched.now, self.store, self.published.append)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class SpecRegistryTest(UcmsUnitBase):
    def test_bridge_spec_after_grace(self):
        self.assertIsNone(self.registry.attribute_spec("SIMPLE_TEMP_SENSOR", "cabin_temp"))
        self.registry.expire_grace()
        spec, provider = self.registry.attribute_spec("SIMPLE_TEMP_SENSOR", "cabin_temp")
        self.assertIs(provider, SpecProvider.BRIDGE)
        self.assertEqual(spec.max_limit, 50.0)
        self.assertEqual([type(p).__name__ for p in self.published],
                         ["MonitoredAttributeSpecification", "MonitoredResourceSpecification"])

    def test_resource_spec_within_grace_wins(self):
        self.registry.on_attribute_spec(resource_spec())
        self.registry.expire_grace()
        spec, provider = self.registry.attribute_spec("SIMPLE_TEMP_SENSOR", "cabin_temp")
        self.assertIs(provider, SpecProvider.RESOURCE)
        self.assertEqual(spec.max_limit, 45.0)
        self.assertEqual(self.published, [])

    def test_late_resource_spec_replaces_bridge(self):
        self.registry.expire_grace()
        self.registry.on_attribute_spec(resource_spec())
        _, provider = self.registry.attribute_spec("SIMPLE_TEMP_SENSOR", "cabin_temp")
        self.assertIs(provider, SpecProvider.RESOURCE)
        rows = self.store.conn.execute("SELECT provider, rationale FROM spec_adoption").fetchall()
        self.assertEqual([r[0] for r in rows], ["BRIDGE", "RESOURCE"])
        self.assertIn("리소스 발행 우선", rows[1][1])


class FaultEventDeriverTest(UcmsUnitBase):
    SPEC = FaultCodeSpecification("V", "F01", 0x1A01, "CAN 타임아웃", BitType.CBIT, Severity.HIGH, ClearPolicy.AUTO)

    def make(self):
        events, disposed, logs = [], [], []
        d = FaultEventDeriver(
            self.store, self.registry, events.append, disposed.append,
            lambda: self.sched.now, self.sched.call_later, 1.0, lambda lv, m: logs.append(m),
        )
        return d, events, disposed, logs

    def code(self, state, t=0.0):
        return FaultCode("V", "F01", state, BitType.CBIT, t)

    def test_lifecycle_with_latch(self):
        self.registry.on_fault_spec(self.SPEC)
        d, events, disposed, _ = self.make()
        d.on_fault_code(self.code(FaultState.ACTIVE))
        d.on_fault_code(self.code(FaultState.ACTIVE))                     # 중복 → 무시
        d.on_fault_code(self.code(FaultState.LATCHED))
        d.on_fault_code(self.code(FaultState.INACTIVE))
        self.assertEqual([(e.state, e.latched) for e in events],
                         [(EventState.ACTIVE, False), (EventState.ACTIVE, True), (EventState.CLEARED, False)])
        self.assertEqual(len({e.event_id for e in events}), 1)
        self.assertEqual(len(disposed), 1)
        self.assertEqual(read_event_ledger(self.db),
                         {("Fault_Event", "FE-00001", t) for t in ("ACTIVE", "LATCHED", "CLEARED")})

    def test_waits_for_spec_then_uses_it(self):
        d, events, _, _ = self.make()
        d.on_fault_code(self.code(FaultState.ACTIVE))
        self.assertEqual(events, [])
        self.registry.on_fault_spec(self.SPEC)
        d.on_fault_spec(self.SPEC)
        self.assertEqual(events[0].description, "CAN 타임아웃")

    def test_spec_timeout_creates_unknown_event(self):
        d, events, _, logs = self.make()
        d.on_fault_code(self.code(FaultState.ACTIVE))
        d.on_fault_code(self.code(FaultState.INACTIVE))                   # 보류 중 해제도 순서대로 처리
        self.sched.run_for(1.5)
        self.assertEqual([e.state for e in events], [EventState.ACTIVE, EventState.CLEARED])
        self.assertEqual(events[0].severity, "unknown")
        self.assertTrue(any("사양 미수신" in m for m in logs))

    def test_restore_after_restart(self):
        self.registry.on_fault_spec(self.SPEC)
        d, _, _, _ = self.make()
        d.on_fault_code(self.code(FaultState.ACTIVE))
        d2, events2, _, _ = self.make()                                   # 같은 DB로 재기동
        d2.restore()
        self.assertEqual([e.event_id for e in events2], ["FE-00001"])
        d2.on_fault_code(self.code(FaultState.ACTIVE))                    # TRANSIENT_LOCAL 재생 → 중복 생성 없음
        self.assertEqual(len(events2), 1)
        (n,) = self.store.conn.execute("SELECT COUNT(*) FROM fault_event").fetchone()
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
