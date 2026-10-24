"""웹 HMI: 실행기 명령·상태와 HTTP API."""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ucmssim.config import load_configs
from ucmssim.gui.runtime import CommandError, SimRuntime
from ucmssim.gui.server import make_server


class GuiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 배속 0 (일시정지): 시험에서는 advance() 로만 진행한다
        self.rt = SimRuntime(load_configs(), Path(self.tmp.name) / "gui.sqlite", speed=0.0)
        self.rt.start()
        self.rt.advance(8.0)

    def tearDown(self):
        self.rt.stop()
        self.tmp.cleanup()

    def vcu(self, snap):
        return next(r for r in snap["hmi"]["resources"] if r["resource_id"] == "UGV_VCU_01")


class RuntimeTest(GuiTestBase):
    def test_snapshot_is_json_and_shows_hmi_state(self):
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
        self.assertEqual([f["fault_code_id"] for f in snap["hmi"]["faults"]], ["F01"])
        self.rt.command("can_block", {"on": False})

        self.rt.command("overtemp")
        with self.assertRaises(CommandError):
            self.rt.command("oscillate")                  # 같은 변수(motor_temp) 프로파일 진행 중
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


class HttpTest(GuiTestBase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.rt, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def test_page_and_state(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("UCMS 간이 HMI", body.decode("utf-8"))
        status, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("hmi", json.loads(body))

    def test_command_endpoint(self):
        payload = json.dumps({"action": "speed", "args": {"kph": 40}})
        status, body = self.request("POST", "/api/cmd", payload, {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["ok"]), (200, True))
        self.assertEqual(self.rt.tb.vcu.speed_setpoint_kph, 40.0)
        status, _ = self.request("POST", "/api/cmd", payload, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)                      # JSON 이외 거부 (교차 사이트 단순 요청 차단)
        status, body = self.request("POST", "/api/cmd", json.dumps({"action": "nope"}),
                                    {"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["ok"]), (400, False))
        status, _ = self.request("GET", "/etc/passwd")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
