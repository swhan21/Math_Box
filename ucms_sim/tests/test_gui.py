"""간이 HMI GUI: 실행기(화면 없이)와 tkinter 화면 (tkinter·디스플레이가 있을 때만)."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from ucmssim.config import load_configs
from ucmssim.gui.runtime import CommandError, SimRuntime

try:
    import tkinter as tk
    _root = tk.Tk()
    _root.destroy()
    HAS_DISPLAY = True
except Exception:                                   # tkinter 미설치 또는 디스플레이 없음
    HAS_DISPLAY = False


class RuntimeBase(unittest.TestCase):
    speed = 0.0                                     # 일시정지: advance() 로만 진행

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = SimRuntime(load_configs(), Path(self.tmp.name) / "gui.sqlite", speed=self.speed)
        self.rt.start()
        self.rt.advance(8.0)

    def tearDown(self):
        self.rt.stop()
        self.tmp.cleanup()

    def vcu(self, snap):
        return next(r for r in snap["hmi"]["resources"] if r["resource_id"] == "UGV_VCU_01")


class RuntimeTest(RuntimeBase):
    def test_snapshot_is_plain_data(self):
        snap = self.rt.snapshot()
        json.dumps(snap)
        self.assertEqual(self.vcu(snap)["health"], "GREEN")
        self.assertEqual(snap["harness"]["vcu"], "운용")
        self.assertLessEqual({"C01", "C02", "C03", "C04"}, {t["characteristic_id"] for t in snap["hmi"]["trends"]})
        self.assertTrue(any(u["usage_id"] == "U01" for u in snap["hmi"]["usage"]))

    def test_fault_injection_and_operator_clear(self):
        self.rt.command("can_block", {"on": True})
        self.rt.advance(1.0)
        snap = self.rt.snapshot()
        self.assertEqual(self.vcu(snap)["health"], "RED")
        self.assertEqual([(f["fault_code_id"], f["bit_type"]) for f in snap["hmi"]["faults"]], [("F01", "CBIT")])
        self.rt.command("can_block", {"on": False})

        self.rt.command("overtemp")
        with self.assertRaises(CommandError):
            self.rt.command("oscillate")              # 같은 변수(motor_temp) 프로파일 진행 중
        self.rt.advance(60.0)
        latched = [f for f in self.rt.snapshot()["hmi"]["faults"] if f["latched"]]
        self.assertEqual([f["fault_code_id"] for f in latched], ["F02"])
        self.rt.command("clear_fault", {"resource_id": "UGV_VCU_01", "fault_code_id": "F02"})
        self.rt.advance(1.0)
        snap = self.rt.snapshot()
        self.assertEqual(snap["hmi"]["faults"], [])
        self.assertEqual(self.vcu(snap)["health"], "GREEN")

    def test_ucms_restart_restores_state(self):
        self.rt.command("can_block", {"on": True})
        self.rt.advance(1.0)
        self.rt.command("ucms_restart")
        self.assertEqual(self.rt.snapshot()["harness"]["ucms"], "정지 (재기동 대기)")
        with self.assertRaises(CommandError):
            self.rt.command("ucms_restart")
        self.rt.advance(4.0)
        snap = self.rt.snapshot()
        self.assertEqual(snap["harness"]["ucms"], "동작")
        self.assertEqual([f["event_id"] for f in snap["hmi"]["faults"]], ["FE-00001"])

    def test_invalid_commands(self):
        for action, args in (("nope", {}), ("speed", {"kph": 500}), ("can_block", {"on": "yes"}),
                             ("speed", {"wrong": 1})):
            with self.assertRaises(CommandError, msg=action):
                self.rt.command(action, args)


class PumpTest(RuntimeBase):
    speed = 10.0

    def test_pump_follows_wall_clock_times_speed(self):
        self.rt.pump()                                # 기준점 설정
        t0 = self.rt.tb.now
        time.sleep(0.15)
        self.rt.pump()
        self.assertAlmostEqual(self.rt.tb.now - t0, 1.5, delta=0.6)
        self.rt.command("sim_speed", {"factor": 0})
        t1 = self.rt.tb.now
        time.sleep(0.05)
        self.rt.pump()
        self.rt.pump()
        self.assertEqual(self.rt.tb.now, t1)          # 일시정지


@unittest.skipUnless(HAS_DISPLAY, "tkinter 또는 디스플레이 없음")
class AppTest(RuntimeBase):
    def setUp(self):
        super().setUp()
        from ucmssim.gui.app import HmiApp
        os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
        self.root = tk.Tk()
        self.app = HmiApp(self.root, self.rt, "dark")
        self.root.update()

    def tearDown(self):
        self.root.after_cancel(self.app._after_id)
        self.root.destroy()
        super().tearDown()

    def step(self, sec):
        self.rt.advance(sec)
        self.app.refresh()
        self.root.update()

    def test_widgets_drive_simulation(self):
        self.app.toggle_btns["can_block"].invoke()    # 체크버튼 클릭 → FI-01
        self.step(1.0)
        rows = [self.app.fault_tree.item(i, "values") for i in self.app.fault_tree.get_children()]
        self.assertEqual([(r[0], r[2], r[3]) for r in rows], [("F01", "CBIT", "활성")])
        self.assertTrue(self.app.clear_btn.instate(["disabled"]))   # 자동 해제 고장은 운용자 해제 불가
        self.app.toggle_btns["can_block"].invoke()
        self.step(3.0)

        self.app.profile_btns["overtemp"].invoke()
        self.assertTrue(self.app.profile_btns["overtemp"].instate(["disabled"]))
        self.step(60.0)
        self.assertTrue(self.app.clear_btn.instate(["!disabled"]))  # 래치 고장 하나 → 자동 선택
        self.app.clear_btn.invoke()
        self.step(1.0)
        self.assertEqual(self.app.fault_tree.get_children(), ())
        self.assertEqual(self.vcu(self.rt.snapshot())["health"], "GREEN")

    def test_charts_table_and_theme(self):
        self.step(30.0)
        self.assertIn("UGV_VCU_01/C01", self.app.charts)
        chart = self.app.charts["UGV_VCU_01/C01"]
        chart.hover_x = chart.canvas.winfo_width() * 0.9
        chart._draw_hover()
        self.assertTrue(chart.canvas.find_withtag("hover"))
        self.app._toggle_table()
        self.step(0.5)
        self.assertGreaterEqual(len(self.app.trend_tree.get_children()), 5)
        self.app.toggle_theme()
        self.step(0.5)
        self.assertEqual(self.app.th.name, "light")
        self.assertTrue(self.app.show_table)


if __name__ == "__main__":
    unittest.main()
