"""T1 단위시험: VCU UCM 어댑터 모듈 (DDS 없이)."""

import unittest

from ucmssim.config import (
    CharacteristicDef,
    FaultDef,
    HealthCondition,
    HealthRule,
    UsageDef,
    load_configs,
)
from ucmssim.topics import BitType, ClearPolicy, FaultState, Health, Severity
from ucmssim.vcu.adapter import CharacteristicSampler, FaultMapper, HealthEvaluator, UsageCounter
from ucmssim.vcu.channel import DiagnosticChannel, DtcRecord
from ucmssim.vcu.control import Debounce
from ucmssim.vcu.nvm import NvmStore

FAULTS = (
    FaultDef("F01", 0x01, "auto", BitType.CBIT, Severity.HIGH, ClearPolicy.AUTO),
    FaultDef("F02", 0x02, "latch", BitType.CBIT, Severity.HIGH, ClearPolicy.LATCH),
    FaultDef("F04", 0x04, "medium", BitType.CBIT, Severity.MEDIUM, ClearPolicy.AUTO),
    FaultDef("F06", 0x06, "restart", BitType.PBIT, Severity.HIGH, ClearPolicy.RESTART),
)
RULE = HealthRule(
    (HealthCondition("pbit_failed"), HealthCondition("any_active_fault_with_severity", Severity.HIGH)),
    Health.GREEN,
)


class FaultMapperTest(unittest.TestCase):
    def setUp(self):
        self.m = FaultMapper(FAULTS)

    def test_auto_clear(self):
        self.assertEqual(self.m.on_dtc(0x01, True), "F01")
        self.assertIs(self.m.states["F01"], FaultState.ACTIVE)
        self.assertIsNone(self.m.on_dtc(0x01, True))                         # 중복 검출은 변화 없음
        self.assertEqual(self.m.on_dtc(0x01, False), "F01")
        self.assertIs(self.m.states["F01"], FaultState.INACTIVE)

    def test_latch_and_operator_clear(self):
        self.m.on_dtc(0x02, True)
        self.assertFalse(self.m.operator_clear("F02"))                       # 조건 지속 중 해제 거부
        self.m.on_dtc(0x02, False)
        self.assertIs(self.m.states["F02"], FaultState.LATCHED)
        self.assertTrue(self.m.operator_clear("F02"))
        self.assertIs(self.m.states["F02"], FaultState.INACTIVE)

    def test_latched_reoccurrence_goes_active(self):
        self.m.on_dtc(0x02, True)
        self.m.on_dtc(0x02, False)
        self.assertEqual(self.m.on_dtc(0x02, True), "F02")
        self.assertIs(self.m.states["F02"], FaultState.ACTIVE)

    def test_restart_policy_holds_until_reboot(self):
        self.m.on_dtc(0x06, True)
        self.assertIsNone(self.m.on_dtc(0x06, False))
        self.assertIs(self.m.states["F06"], FaultState.ACTIVE)
        self.assertFalse(self.m.operator_clear("F06"))

    def test_unknown_dtc_is_recorded(self):
        self.assertIsNone(self.m.on_dtc(0x99, True))
        self.assertEqual(self.m.unknown_dtcs, {0x99})


class HealthEvaluatorTest(unittest.TestCase):
    def setUp(self):
        self.m = FaultMapper(FAULTS)
        self.h = HealthEvaluator(RULE, self.m.defs)

    def test_rules(self):
        self.assertIs(self.h.evaluate(False, False, self.m.states), Health.GREEN)   # PBIT 진행 중
        self.assertIs(self.h.evaluate(True, False, self.m.states), Health.RED)      # PBIT 실패
        self.assertIs(self.h.evaluate(True, True, self.m.states), Health.GREEN)
        self.m.on_dtc(0x04, True)                                                    # medium → GREEN 유지
        self.assertIs(self.h.evaluate(True, True, self.m.states), Health.GREEN)
        self.m.on_dtc(0x02, True)
        self.m.on_dtc(0x02, False)                                                   # 래치도 활성으로 본다
        self.assertIs(self.h.evaluate(True, True, self.m.states), Health.RED)


class SamplerTest(unittest.TestCase):
    def test_rate_and_invalid_values(self):
        chars = (CharacteristicDef("C01", "a", "degC", 1.0, "a", None),
                 CharacteristicDef("C04", "b", "degC", 0.2, "b", None))
        s = CharacteristicSampler(chars, t_start=0.0)
        published = {"C01": 0, "C04": 0}
        for k in range(101):                                               # 10 s, 100 ms 태스크
            t = k * 0.1
            sig = {"a": None if 3.0 <= t < 5.0 else 1.0, "b": 2.0}
            for c, _ in s.due(t, sig):
                published[c.id] += 1
        self.assertEqual(published, {"C01": 11 - 2, "C04": 3})
        self.assertEqual(s.invalid_skips["C01"], 2)


class UsageCounterTest(unittest.TestCase):
    DEFS = (UsageDef("U01", "powered_on_time", "s", 10.0, True),
            UsageDef("U02", "emergency_stop_count", "count", None, True))

    def test_accumulate_and_persist(self):
        nvm = NvmStore()
        u = UsageCounter(self.DEFS, nvm, lambda m: None)
        self.assertEqual(sorted(u.due(0.0)), ["U01", "U02"])                # 기동 시 1회
        u.tick(12.0)
        u.on_event("emergency_stop")
        self.assertEqual(sorted(u.due(10.0)), ["U01", "U02"])
        u.persist()
        again = UsageCounter(self.DEFS, nvm, lambda m: None)
        self.assertEqual(again.values, {"U01": 12.0, "U02": 1.0})

    def test_corrupt_block_resets(self):
        nvm = NvmStore()
        nvm.write("usage", {"U01": 50.0, "U02": 3.0})
        nvm.corrupt("usage")
        logs = []
        u = UsageCounter(self.DEFS, nvm, logs.append)
        self.assertFalse(u.load_ok)
        self.assertEqual(u.values, {"U01": 0.0, "U02": 0.0})
        self.assertEqual(len(logs), 1)


class ChannelAndDebounceTest(unittest.TestCase):
    def test_channel_never_blocks_and_counts_overflow(self):
        ch = DiagnosticChannel(size=4)
        for i in range(10):
            ch.post_dtc(i, True, float(i))
        self.assertEqual(ch.overflow, 6)
        self.assertEqual([r.dtc for r in ch.drain()], [6, 7, 8, 9])
        self.assertIsInstance(DtcRecord(0.0, 1, True), DtcRecord)

    def test_debounce(self):
        d = Debounce(set_s=0.5, clear_s=1.0)
        self.assertFalse(d.update(True, 0.0))
        self.assertFalse(d.update(True, 0.4))
        self.assertTrue(d.update(True, 0.5))
        self.assertTrue(d.update(False, 1.0))
        self.assertTrue(d.update(True, 1.5))                               # 해제 대기 중 재발 → 리셋
        self.assertTrue(d.update(False, 2.0))
        self.assertFalse(d.update(False, 3.0))


class ConfigTest(unittest.TestCase):
    def test_default_config_loads(self):
        cfg = load_configs()
        self.assertEqual(len(cfg.vcu.faults), 6)
        self.assertEqual(len(cfg.vcu.characteristics), 4)
        self.assertEqual(cfg.bridge.grace_s, 5.0)


if __name__ == "__main__":
    unittest.main()
