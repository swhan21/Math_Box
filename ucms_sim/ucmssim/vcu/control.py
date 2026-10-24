"""기존 VCU 제어 SW + BIT·진단 모사.

설계 방침 '최소 침습'(1장)에 따라 이 모듈은 '기존 코드'로 간주한다.
UCM 연동부는 `# [UCM 연동]` 표시가 붙은 진단 채널 쓰기 호출 몇 줄뿐이며 (6.3절),
채널이 없거나(UCM 미연동) 어댑터가 멈춰도 제어 동작은 동일하다 (FI-13에서 확인).
"""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass

from ..plant import UgvPlant, volt_to_temp
from .channel import DiagnosticChannel
from .nvm import NvmStore

# 기존 VCU 진단 목록 (SC1 매핑률 산출 기준). ICD 7.1절 매핑 대상 + 매핑 제외 항목.
DTC_TABLE: dict[int, str] = {
    0x1A01: "인버터 CAN 메시지 타임아웃",
    0x1A02: "구동모터 과온",
    0x1A03: "배터리 저전압",
    0x1A04: "모터 온도센서 단선·범위 이탈",
    0x1A05: "휠속도 센서 불일치",
    0x1A06: "비휘발 메모리 CRC 불일치",
    0x1C02: "조향 액추에이터 응답 지연",
    0x1F01: "디버그 카운터 오버플로",
}

DEFAULT_CALIBRATION = {"version": 1, "speed_kp": 40.0, "speed_ki": 10.0}


@dataclass(frozen=True)
class BitCalibration:
    inverter_can_timeout_s: float = 0.1
    motor_overtemp_set_c: float = 150.0
    motor_overtemp_clear_c: float = 140.0
    motor_overtemp_debounce_s: float = 0.5
    battery_uv_set_v: float = 300.0
    battery_uv_clear_v: float = 310.0
    battery_uv_debounce_s: float = 0.5
    temp_sensor_min_v: float = 0.2
    temp_sensor_max_v: float = 4.8
    temp_sensor_debounce_s: float = 0.1
    wheel_mismatch_kph: float = 3.0
    wheel_min_speed_kph: float = 5.0
    wheel_mismatch_debounce_s: float = 0.5
    clear_debounce_s: float = 1.0


@dataclass(frozen=True)
class ControlGains:
    kp: float = 40.0
    ki: float = 10.0
    max_torque_nm: float = 400.0
    max_regen_nm: float = -150.0


@dataclass(frozen=True)
class PbitResult:
    passed: bool
    failed_blocks: tuple[str, ...]


class Debounce:
    """연속 set_s 동안 이상이면 검출, 연속 clear_s 동안 정상이면 해제."""

    def __init__(self, set_s: float, clear_s: float) -> None:
        self.set_s = set_s
        self.clear_s = clear_s
        self.active = False
        self._since: float | None = None

    def update(self, bad: bool, t: float) -> bool:
        if bad == self.active:
            self._since = None
            return self.active
        if self._since is None:
            self._since = t
        need = self.set_s if bad else self.clear_s
        if t - self._since >= need - 1e-9:
            self.active = bad
            self._since = None
        return self.active


class VcuControlSoftware:
    def __init__(
        self,
        plant: UgvPlant,
        cal: BitCalibration,
        gains: ControlGains,
        channel: DiagnosticChannel | None = None,
        profile: bool = False,
    ) -> None:
        self.plant = plant
        self.cal = cal
        self.gains = gains
        self.channel = channel
        self.profile = profile
        self.speed_setpoint_kph = 0.0
        self.dtc_active: set[int] = set()
        self.motor_temp: float | None = None
        self.inverter_temp: float | None = None
        self.battery_voltage = 0.0
        self.vcu_temp = 0.0
        self.speed_kph = 0.0
        self.step_wall_s: list[float] = []
        self._integ = 0.0
        self._estop_prev = False
        self._last_inv_rx = 0.0
        self._trace = hashlib.blake2b(digest_size=16)
        self._deb = {
            0x1A01: Debounce(0.0, cal.clear_debounce_s),
            0x1A02: Debounce(cal.motor_overtemp_debounce_s, cal.clear_debounce_s),
            0x1A03: Debounce(cal.battery_uv_debounce_s, cal.clear_debounce_s),
            0x1A04: Debounce(cal.temp_sensor_debounce_s, cal.clear_debounce_s),
            0x1A05: Debounce(cal.wheel_mismatch_debounce_s, cal.clear_debounce_s),
        }

    # -------------------------------------------------------------- 기동 시 자체점검

    def run_pbit(self, nvm: NvmStore, t: float) -> PbitResult:
        failed = []
        _, ok = nvm.read("calibration")
        if not ok:
            failed.append("calibration")
            nvm.write("calibration", DEFAULT_CALIBRATION)   # 기본값으로 복구 후 재기록
        result = PbitResult(passed=not failed, failed_blocks=tuple(failed))
        self._set_dtc(0x1A06, not result.passed, t)
        if self.channel is not None:                        # [UCM 연동]
            self.channel.post_pbit(result.passed, t)
        return result

    def start(self, t: float) -> None:
        self._last_inv_rx = t

    # -------------------------------------------------------------- 제어 주기

    def step(self, t: float, dt: float) -> None:
        w0 = time.perf_counter() if self.profile else 0.0
        p = self.plant
        cal = self.cal

        inv = p.inverter_status_frame(t)
        if inv is not None:
            self._last_inv_rx = t
            self.inverter_temp = inv.inverter_temp_c
        self.battery_voltage = p.bms_frame(t).voltage_v
        v_raw = p.motor_temp_sensor_v()
        sensor_in_range = cal.temp_sensor_min_v <= v_raw <= cal.temp_sensor_max_v
        self.motor_temp = volt_to_temp(v_raw) if sensor_in_range else None
        left, right = p.wheel_speeds_kph()
        self.speed_kph = 0.5 * (left + right)
        self.vcu_temp = p.vcu_board_temp()

        estop = p.estop_pressed
        if estop and not self._estop_prev and self.channel is not None:   # [UCM 연동]
            self.channel.post_event("emergency_stop", t)
        self._estop_prev = estop

        torque = self._speed_control(dt, estop)
        p.brake = estop
        p.receive_torque_cmd(t, torque)
        self._trace.update(struct.pack("<d", torque))

        self._bit(t, left, right, v_raw, sensor_in_range)

        if self.channel is not None:                                       # [UCM 연동]
            self.channel.publish_signals(t, {
                "motor_temp": self.motor_temp,
                "battery_voltage": self.battery_voltage,
                "inverter_temp": None if 0x1A01 in self.dtc_active else self.inverter_temp,
                "vcu_temp": self.vcu_temp,
            })
        if self.profile:
            self.step_wall_s.append(time.perf_counter() - w0)

    def trace_digest(self) -> str:
        """지금까지 출력한 토크 지령열의 해시 (제어 동작 동일성 비교용)."""
        return self._trace.hexdigest()

    def _speed_control(self, dt: float, estop: bool) -> float:
        g = self.gains
        if estop or (self.speed_setpoint_kph <= 0.0 and self.speed_kph < 0.5):
            self._integ = 0.0
            return 0.0
        err = self.speed_setpoint_kph - self.speed_kph
        upper = g.max_torque_nm * (0.5 if 0x1A02 in self.dtc_active else 1.0)   # 과온 시 출력 제한
        u = g.kp * err + g.ki * self._integ
        if g.max_regen_nm < u < upper:
            self._integ += err * dt
        return min(max(u, g.max_regen_nm), upper)

    # -------------------------------------------------------------- CBIT

    def _bit(self, t: float, left: float, right: float, v_raw: float, sensor_in_range: bool) -> None:
        cal = self.cal
        self._eval(0x1A01, t - self._last_inv_rx > cal.inverter_can_timeout_s, t)
        self._eval(0x1A04, not sensor_in_range, t)
        if self.motor_temp is not None and 0x1A04 not in self.dtc_active:
            limit = cal.motor_overtemp_clear_c if 0x1A02 in self.dtc_active else cal.motor_overtemp_set_c
            self._eval(0x1A02, self.motor_temp > limit, t)
        limit = cal.battery_uv_clear_v if 0x1A03 in self.dtc_active else cal.battery_uv_set_v
        self._eval(0x1A03, self.battery_voltage < limit, t)
        if self.speed_kph >= cal.wheel_min_speed_kph:
            self._eval(0x1A05, abs(left - right) > cal.wheel_mismatch_kph, t)

    def _eval(self, dtc: int, bad: bool, t: float) -> None:
        self._set_dtc(dtc, self._deb[dtc].update(bad, t), t)

    def _set_dtc(self, dtc: int, active: bool, t: float) -> None:
        if active == (dtc in self.dtc_active):
            return
        if active:
            self.dtc_active.add(dtc)
        else:
            self.dtc_active.discard(dtc)
        if self.channel is not None:                                       # [UCM 연동]
            self.channel.post_dtc(dtc, active, t)
