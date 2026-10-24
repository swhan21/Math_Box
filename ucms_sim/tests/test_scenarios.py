"""T2 SIL 통합시험: 시험대 전체로 시나리오 실행 (빠른 설정)."""

import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ucmssim.config import ConfigError, load_configs
from ucmssim.harness.report import evaluate
from ucmssim.harness.scenarios import SCENARIOS, RunContext
from ucmssim.harness.testbed import Testbed
from ucmssim.topics import Health, MonitoredAttributeSpecification, SpecProvider

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_configs()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, sid, run=0, cfg=None, fi11=120.0):
        ctx = RunContext(cfg or self.cfg, sid, run, 1000 + run, self.dir, fi11)
        return SCENARIOS[sid].func(ctx)

    def test_all_scenarios_pass_with_default_config(self):
        results = [self.run_scenario(sid) for sid in SCENARIOS]
        failed = [(r.scenario_id, r.note) for r in results if not r.passed]
        self.assertEqual(failed, [])
        self.assertTrue(all(r.ledger_missing == 0 and r.ledger_extra == 0 for r in results))
        verdicts = {v.id: v.verdict for v in evaluate(self.cfg, results)}
        self.assertEqual(verdicts["SC3"], "충족")
        self.assertEqual(verdicts["SC8"], "보류")                           # 기준값 TBD

    def test_latency_within_limit(self):
        for sid in ("FI-01", "FI-02", "FI-03"):
            r = self.run_scenario(sid)
            self.assertLess(r.latency_s, 1.0, sid)
            self.assertGreater(r.latency_s, 0.0, sid)

    def test_harness_detects_latency_violation(self):
        """음성 시험: BIT 디바운스를 2 s로 늘리면 SC3이 미충족으로 판정돼야 한다."""
        slow_bit = replace(self.cfg.sim.bit, motor_overtemp_debounce_s=2.0)
        cfg = replace(self.cfg, sim=replace(self.cfg.sim, bit=slow_bit))
        r = self.run_scenario("FI-02", cfg=cfg)
        self.assertGreater(r.latency_s, 2.0)
        verdicts = {v.id: v.verdict for v in evaluate(cfg, [r])}
        self.assertEqual(verdicts["SC3"], "미충족")

    def test_fault_cleared_while_ucms_down_is_reconciled(self):
        tb = Testbed(self.cfg, 5, str(self.dir / "down.sqlite"))
        tb.bring_up()
        t = tb.now
        tb.plant.faults.inverter_can_blocked = True
        self.assertIsNotNone(tb.wait_fault("F01", "ACTIVE", t, 5.0))
        tb.stop_ucms()
        tb.plant.faults.inverter_can_blocked = False                     # UCMS 정지 중 해소
        tb.run_for(3.0)
        t_restart = tb.now
        tb.start_new_ucms()
        cleared = tb.wait_fault("F01", "CLEARED", t_restart, 2.0)
        self.assertIsNotNone(cleared)
        self.assertEqual(cleared[1].event_id, "FE-00001")
        self.assertIsNotNone(tb.wait_health_is(Health.GREEN, t_restart, 2.0))
        tb.close()

    def test_simple_device_with_own_spec_is_not_bridged(self):
        spec = MonitoredAttributeSpecification(
            "SIMPLE_TEMP_SENSOR", "cabin_temp", "cabin_temp", "degC", 1.0, 45.0, None, 1.0, 3, 3,
            SpecProvider.RESOURCE, "장비 자체 사양",
        )
        tb = Testbed(self.cfg, 6, str(self.dir / "own.sqlite"), device_spec=spec, device_spec_delay_s=1.0)
        tb.bring_up()
        adopted = tb.ucms.registry.attribute_spec("SIMPLE_TEMP_SENSOR", "cabin_temp")
        self.assertIs(adopted[1], SpecProvider.RESOURCE)
        self.assertEqual(adopted[0].max_limit, 45.0)
        tb.close()

    def test_vcu_reboot_clears_latched_fault(self):
        tb = Testbed(self.cfg, 7, str(self.dir / "reboot.sqlite"))
        tb.bring_up()
        t = tb.now
        tb.plant.set_override("motor_temp", lambda tt: 160.0 if tt < t + 3.0 else (80.0 if tt < t + 6.0 else None))
        self.assertIsNotNone(tb.wait_fault("F02", "LATCHED", t, 10.0))
        t_reboot = tb.now
        tb.vcu.reboot()                                                   # 래치 → 재기동으로 해제
        self.assertIsNotNone(tb.wait_fault("F02", "CLEARED", t_reboot, 3.0))
        tb.close()

    def test_tbd_value_is_rejected(self):
        bad = self.dir / "cfg"
        shutil.copytree(CONFIG_DIR, bad)
        path = bad / "vcu_ucm.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("max: 140", "max: TBD"), encoding="utf-8")
        with self.assertRaises(ConfigError) as cm:
            load_configs(bad)
        self.assertIn("TBD", str(cm.exception))
        self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
