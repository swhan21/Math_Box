"""HMI GUI용 실시간 실행기 (GUI 프레임워크와 무관한 부분).

시험대(Testbed)를 벽시계에 맞춰(배속 적용) 진행시키고, HMI 화면 상태 조회와 명령을 받는다.
GUI 이벤트 루프가 pump() 를 주기적으로 호출하므로 별도 스레드가 없다 (tkinter는 스레드 안전하지 않음).
명령은 두 종류다.
  - HMI(O-role) 명령: 래치 고장 운용자 해제
  - 시험 하니스 명령: 속도 지령, 고장 주입(FI-01~10), VCU·UCMS 재기동, 배속
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from ..config import Configs
from ..harness.profiles import oscillation, piecewise, ramp_hold_return
from ..harness.testbed import Testbed
from ..plant import Profile

# 프로파일 주입 명령 → 덮어쓰는 플랜트 변수 (같은 변수에 두 프로파일을 동시에 걸지 않는다)
PROFILE_VARIABLES = {
    "overtemp": "motor_temp",
    "undervolt": "battery_voltage",
    "inverter_hot": "inverter_temp",
    "oscillate": "motor_temp",
    "cabin_hot": "cabin_temp",
}

MAX_STEP_S = 1.0        # pump() 한 번에 진행하는 최대 모의 시간 (화면 응답성 유지)


class CommandError(ValueError):
    pass


def _on(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise CommandError("on 값은 true/false 여야 함")


def _number(value: Any, lo: float, hi: float, what: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise CommandError(f"{what}: 숫자가 아님") from None
    if not lo <= v <= hi:
        raise CommandError(f"{what}: {lo:g}~{hi:g} 범위 밖")
    return v


class SimRuntime:
    def __init__(self, cfg: Configs, db_path: str | Path, speed: float = 1.0, seed: int = 7,
                 trend_len: int = 180) -> None:
        self.cfg = cfg
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.tb = Testbed(cfg, seed, str(db_path), hmi_trend_len=trend_len)
        self.speed = speed
        self.log: deque[tuple[float, str]] = deque(maxlen=40)
        self.error: str | None = None
        self._anchor: tuple[float, float, float] | None = None     # (벽시계, 모의 시각, 배속)
        self._ucms_restart_pending = False
        self._commands: dict[str, Callable[..., str]] = {
            "clear_fault": self._clear_fault,
            "sim_speed": self._sim_speed,
            "speed": self._vehicle_speed,
            "can_block": self._toggle("inverter_can_blocked", "FI-01 인버터 CAN 차단"),
            "sensor_open": self._toggle("motor_temp_sensor_open", "FI-04 모터 온도센서 단선"),
            "wheel_stuck": self._toggle("wheel_left_stuck", "FI-05 좌측 휠속도 센서 고착"),
            "estop": self._estop,
            "overtemp": self._overtemp,
            "undervolt": self._undervolt,
            "inverter_hot": self._inverter_hot,
            "oscillate": self._oscillate,
            "cabin_hot": self._cabin_hot,
            "nvm_reboot": self._nvm_reboot,
            "vcu_reboot": self._vcu_reboot,
            "ucms_restart": self._ucms_restart,
        }

    # -------------------------------------------------------------- 실행

    def start(self) -> None:
        self.tb.start()
        self.tb.vcu.set_speed(20.0)
        self._log("시뮬레이션 시작, 속도 지령 20 km/h")

    def stop(self) -> None:
        self.tb.close()

    def advance(self, duration: float) -> None:
        """벽시계와 무관하게 모의 시간을 진행한다 (시험·스크립트용)."""
        self.tb.run_for(duration)

    def pump(self) -> None:
        """벽시계 경과 × 배속만큼 모의 시간을 진행한다. GUI가 수십 ms 주기로 호출한다."""
        if self.error is not None:
            return
        wall = time.monotonic()
        if self._anchor is None or self._anchor[2] != self.speed or self.speed <= 0:
            self._anchor = (wall, self.tb.now, self.speed)          # 시작·배속 변경·일시정지 → 기준점 재설정
            return
        wall0, sim0, speed = self._anchor
        target = sim0 + (wall - wall0) * speed
        if target <= self.tb.now:
            return
        try:
            self.tb.sched.run_until(min(target, self.tb.now + MAX_STEP_S))
        except Exception as e:                                       # 시뮬레이션 정지, 화면에 표시
            self.error = f"{type(e).__name__}: {e}"
            self._log(f"시뮬레이션 오류로 정지 — {self.error}")
            return
        if target - self.tb.now > MAX_STEP_S:                       # 계산이 배속을 못 따라감 → 밀린 시간 버림
            self._anchor = (wall, self.tb.now, speed)

    # -------------------------------------------------------------- 조회

    def snapshot(self) -> dict[str, Any]:
        tb = self.tb
        p, vcu = tb.plant, tb.vcu
        if not vcu.powered:
            vcu_state = "전원 차단"
        elif vcu.control is None:
            vcu_state = "부팅 중"
        elif vcu.pbit_result is None:
            vcu_state = "PBIT 중"
        else:
            vcu_state = "운용"
        if tb.ucms.running:
            ucms_state = "동작"
        else:
            ucms_state = "정지 (재기동 대기)" if self._ucms_restart_pending else "정지"
        return {
            "hmi": tb.hmi.snapshot(),
            "harness": {
                "t": tb.now,
                "error": self.error,
                "sim_speed": self.speed,
                "setpoint_kph": vcu.speed_setpoint_kph,
                "toggles": {
                    "can_block": p.faults.inverter_can_blocked,
                    "sensor_open": p.faults.motor_temp_sensor_open,
                    "wheel_stuck": p.faults.wheel_left_stuck,
                    "estop": p.estop_pressed,
                },
                "busy": {name: p.has_override(var) for name, var in PROFILE_VARIABLES.items()},
                "vcu": vcu_state,
                "boot_count": vcu.boot_count,
                "ucms": ucms_state,
                "plant": {
                    "speed_kph": p.speed * 3.6,
                    "motor_temp": p.motor_temp,
                    "inverter_temp": p.inverter_temp,
                    "battery_voltage": p.battery_voltage,
                    "soc": p.soc * 100.0,
                    "vcu_temp": p.vcu_temp,
                    "cabin_temp": p.cabin_temp,
                },
                "log": [{"t": t, "text": text} for t, text in reversed(self.log)],
            },
        }

    # -------------------------------------------------------------- 명령

    def command(self, action: str, args: dict[str, Any] | None = None) -> str:
        fn = self._commands.get(action)
        if fn is None:
            raise CommandError(f"알 수 없는 명령: {action}")
        try:
            message = fn(**(args or {}))
        except TypeError:
            raise CommandError(f"{action}: 인수 오류") from None
        self._log(message)
        return message

    def _log(self, text: str) -> None:
        self.log.append((self.tb.now, text))

    # HMI (O-role)

    def _clear_fault(self, resource_id: str, fault_code_id: str) -> str:
        self.tb.hmi.clear_latched(str(resource_id), str(fault_code_id), operator="HMI 운용자")
        return f"[HMI] {resource_id}/{fault_code_id} 해제 명령 송신"

    # 시험 하니스

    def _sim_speed(self, factor: Any) -> str:
        self.speed = _number(factor, 0.0, 20.0, "배속")
        return f"배속 {self.speed:g}×" if self.speed else "일시정지"

    def _vehicle_speed(self, kph: Any) -> str:
        v = _number(kph, 0.0, 80.0, "속도 지령")
        self.tb.vcu.set_speed(v)
        return f"속도 지령 {v:g} km/h"

    def _toggle(self, flag: str, label: str) -> Callable[..., str]:
        def apply(on: Any) -> str:
            setattr(self.tb.plant.faults, flag, _on(on))
            return f"{label} {'ON' if on else 'OFF'}"
        return apply

    def _estop(self, on: Any) -> str:
        self.tb.plant.estop_pressed = _on(on)
        return f"비상정지 {'작동' if on else '해제'}"

    def _profile(self, action: str, profile: Profile) -> None:
        var = PROFILE_VARIABLES[action]
        if self.tb.plant.has_override(var):
            raise CommandError(f"{var} 프로파일이 이미 진행 중")
        self.tb.plant.set_override(var, profile)

    def _overtemp(self) -> str:
        t, bit = self.tb.now, self.cfg.sim.bit
        peak = bit.motor_overtemp_set_c + 10.0
        self._profile("overtemp", piecewise(ramp_hold_return(t, self.tb.plant.motor_temp, peak, 4.0, 3.0, 80.0)))
        return f"FI-02 모터 과온 프로파일 (→ {peak:g} °C → 80 °C)"

    def _undervolt(self) -> str:
        t, v0 = self.tb.now, self.tb.plant.battery_voltage
        low = self.cfg.sim.bit.battery_uv_set_v - 20.0
        self._profile("undervolt", piecewise(ramp_hold_return(t, v0, low, 10.0, 3.0, v0)))
        return f"FI-03 배터리 전압 강하 (→ {low:g} V → 복귀)"

    def _inverter_hot(self) -> str:
        t, c0 = self.tb.now, self.tb.plant.inverter_temp
        peak = self.cfg.vcu.characteristic("C03").threshold.max + 10.0
        self._profile("inverter_hot", piecewise(ramp_hold_return(t, c0, peak, 2.0, 5.0, c0)))
        return f"C03 인버터 과열 프로파일 (→ {peak:g} °C, 고장코드 없음·임계값만)"

    def _oscillate(self) -> str:
        limit = self.cfg.vcu.characteristic("C01").threshold.max
        self._profile("oscillate", oscillation(limit, 3.0, 3.0, self.tb.now, 60.0, 80.0, 4.0))
        return f"FI-07 C01 임계값 근처 진동 ({limit:g}±3 °C, 60 s)"

    def _cabin_hot(self) -> str:
        fb = self.cfg.bridge.fallbacks[0]
        t, c0 = self.tb.now, self.tb.plant.cabin_temp
        peak = fb.threshold.max + 10.0
        self._profile("cabin_hot", piecewise(ramp_hold_return(t, c0, peak, 2.0, 3.0, c0)))
        return f"FI-10 실내온도 상승 (→ {peak:g} °C, 브리지 사양 판정)"

    def _require_vcu_running(self) -> None:
        if self.tb.vcu.pbit_result is None:
            raise CommandError("VCU가 기동 중이거나 꺼져 있음")

    def _nvm_reboot(self) -> str:
        self._require_vcu_running()
        self.tb.nvm.corrupt("calibration")
        self.tb.vcu.reboot()
        return "FI-06 NVM 손상 후 VCU 재기동"

    def _vcu_reboot(self) -> str:
        self._require_vcu_running()
        self.tb.vcu.reboot()
        return "VCU 재기동"

    def _ucms_restart(self) -> str:
        if not self.tb.ucms.running:
            raise CommandError("UCMS가 이미 정지 상태")
        self.tb.stop_ucms()
        self._ucms_restart_pending = True
        self.tb.sched.call_later(3.0, self._restart_ucms)
        return "FI-08 UCMS 정지 → 3 s 후 재기동"

    def _restart_ucms(self) -> None:
        self._ucms_restart_pending = False
        self.tb.start_new_ucms()
        self._log("UCMS 재기동 완료 (DB 활성 이벤트 복원)")
