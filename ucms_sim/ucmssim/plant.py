"""HILS 실시간 시뮬레이터의 플랜트 모델 모사 (설계안 5.3절).

구동모터·인버터·배터리·센서의 단순 1차 모델과 고장 주입 블록으로 구성된다.
  - 오버라이드: 플랜트 변수(모터 온도 등)를 시험 프로파일로 덮어쓴다 (플랜트 수준 주입)
  - 신호 고장: CAN 차단, 센서 단선, 센서 고착 (통신·신호 수준 주입)
수치는 SIL용 예시값이다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable, Optional

# 시각 t → 값. None 을 돌려주면 오버라이드가 끝나고 모델 값으로 돌아간다.
Profile = Callable[[float], Optional[float]]

GRAVITY = 9.81
AIR_DENSITY = 1.2

# 모터 온도센서 (선형 근사): -40~200 °C → 0.2~4.8 V, 단선 시 풀업 전압
TEMP_SENSOR_C_MIN, TEMP_SENSOR_C_MAX = -40.0, 200.0
TEMP_SENSOR_V_MIN, TEMP_SENSOR_V_MAX = 0.2, 4.8
TEMP_SENSOR_OPEN_V = 4.98


def temp_to_volt(temp_c: float) -> float:
    span = TEMP_SENSOR_C_MAX - TEMP_SENSOR_C_MIN
    v = TEMP_SENSOR_V_MIN + (temp_c - TEMP_SENSOR_C_MIN) / span * (TEMP_SENSOR_V_MAX - TEMP_SENSOR_V_MIN)
    return min(max(v, 0.0), 5.0)


def volt_to_temp(volt: float) -> float:
    span = TEMP_SENSOR_V_MAX - TEMP_SENSOR_V_MIN
    return TEMP_SENSOR_C_MIN + (volt - TEMP_SENSOR_V_MIN) / span * (TEMP_SENSOR_C_MAX - TEMP_SENSOR_C_MIN)


@dataclass(frozen=True)
class PlantParams:
    mass_kg: float = 3000.0
    wheel_radius_m: float = 0.4
    gear_ratio: float = 10.0
    max_motor_power_w: float = 100_000.0
    rolling_coeff: float = 0.02
    drag_area_m2: float = 3.0           # Cd * A
    brake_force_n: float = 15_000.0
    ambient_c: float = 25.0
    motor_copper_k: float = 0.025       # 동손 [W/(Nm)^2]
    motor_iron_k: float = 1.0           # 철손 [W/(rad/s)]
    motor_rth: float = 0.15             # 열저항 [K/W]
    motor_cth: float = 3000.0           # 열용량 [J/K]
    inverter_loss_ratio: float = 0.02
    inverter_rth: float = 0.1
    inverter_cth: float = 2000.0
    battery_capacity_ah: float = 115.0
    battery_r_ohm: float = 0.08
    battery_ocv_min_v: float = 330.0    # SOC 0 개방전압
    battery_ocv_span_v: float = 50.0    # SOC 0→1 개방전압 증가분
    initial_soc: float = 0.9
    aux_power_w: float = 500.0
    drive_efficiency: float = 0.92
    inverter_cmd_timeout_s: float = 0.05
    temp_noise_c: float = 0.2
    voltage_noise_v: float = 0.1
    wheel_noise_kph: float = 0.05


@dataclass
class PlantFaults:
    """신호·통신 수준 고장 주입 스위치."""

    inverter_can_blocked: bool = False      # FI-01: 인버터 CAN 메시지 차단 (양방향)
    motor_temp_sensor_open: bool = False    # FI-04: 모터 온도센서 단선
    wheel_left_stuck: bool = False          # FI-05: 좌측 휠속도 센서 고착


@dataclass(frozen=True)
class InverterFrame:
    t: float
    inverter_temp_c: float
    motor_speed_rads: float
    torque_nm: float


@dataclass(frozen=True)
class BmsFrame:
    t: float
    voltage_v: float
    current_a: float


class UgvPlant:
    OVERRIDABLE = ("motor_temp", "inverter_temp", "battery_voltage", "vcu_temp", "cabin_temp")

    def __init__(self, params: PlantParams | None = None, rng: random.Random | None = None) -> None:
        self.p = params or PlantParams()
        self.rng = rng or random.Random(0)
        self.faults = PlantFaults()
        amb = self.p.ambient_c
        self.speed = 0.0                    # m/s
        self.motor_temp = amb + 20.0
        self.inverter_temp = amb + 10.0
        self.soc = self.p.initial_soc
        self.battery_current = 0.0
        self.battery_voltage = self._ocv()
        self.vcu_temp = amb + 12.0
        self.cabin_temp = amb + 3.0
        self.applied_torque = 0.0
        self.brake = False
        self.estop_pressed = False
        self._torque_cmd = 0.0
        self._cmd_rx_t = -1.0
        self._overrides: dict[str, Profile] = {}
        self._left_frozen: float | None = None

    # -------------------------------------------------------------- 고장 주입 (오버라이드)

    def set_override(self, name: str, profile: Profile) -> None:
        if name not in self.OVERRIDABLE:
            raise KeyError(f"override not supported: {name}")
        self._overrides[name] = profile

    def clear_override(self, name: str) -> None:
        self._overrides.pop(name, None)

    def _ov(self, name: str, t: float, model_value: float) -> float:
        profile = self._overrides.get(name)
        if profile is None:
            return model_value
        v = profile(t)
        if v is None:
            del self._overrides[name]
            return model_value
        return v

    # -------------------------------------------------------------- 액추에이터 입력 (CAN)

    def receive_torque_cmd(self, t: float, torque_nm: float) -> None:
        if self.faults.inverter_can_blocked:
            return
        self._torque_cmd = torque_nm
        self._cmd_rx_t = t

    # -------------------------------------------------------------- 모델 갱신

    def _ocv(self) -> float:
        return self.p.battery_ocv_min_v + self.p.battery_ocv_span_v * self.soc

    def step(self, t: float, dt: float) -> None:
        p = self.p
        amb = p.ambient_c
        omega = self.speed / p.wheel_radius_m * p.gear_ratio

        # 인버터: 지령이 끊기면 안전 상태(토크 0)
        torque = self._torque_cmd if t - self._cmd_rx_t <= p.inverter_cmd_timeout_s else 0.0
        if omega > 1.0 and torque > 0.0:
            torque = min(torque, p.max_motor_power_w / omega)
        self.applied_torque = torque

        # 종방향 동역학 (전진만)
        f_drive = torque * p.gear_ratio / p.wheel_radius_m
        f_roll = p.rolling_coeff * p.mass_kg * GRAVITY if self.speed > 0.01 else 0.0
        f_aero = 0.5 * AIR_DENSITY * p.drag_area_m2 * self.speed ** 2
        f_brake = p.brake_force_n if self.brake and self.speed > 0.0 else 0.0
        accel = (f_drive - f_roll - f_aero - f_brake) / p.mass_kg
        self.speed = max(0.0, self.speed + accel * dt)

        # 열 모델 (1차)
        mech_w = torque * omega
        motor_loss = p.motor_copper_k * torque ** 2 + p.motor_iron_k * omega
        self.motor_temp += (motor_loss - (self.motor_temp - amb) / p.motor_rth) / p.motor_cth * dt
        self.motor_temp = self._ov("motor_temp", t, self.motor_temp)
        inv_loss = p.inverter_loss_ratio * abs(mech_w) + 30.0
        self.inverter_temp += (inv_loss - (self.inverter_temp - amb) / p.inverter_rth) / p.inverter_cth * dt
        self.inverter_temp = self._ov("inverter_temp", t, self.inverter_temp)

        # 배터리
        elec_w = (mech_w / p.drive_efficiency if mech_w >= 0 else mech_w * p.drive_efficiency) + p.aux_power_w
        self.battery_current = elec_w / max(self.battery_voltage, 1.0)
        self.soc = min(max(self.soc - self.battery_current * dt / (p.battery_capacity_ah * 3600.0), 0.0), 1.0)
        self.battery_voltage = self._ov("battery_voltage", t, self._ocv() - self.battery_current * p.battery_r_ohm)

        # 전장품·실내
        self.vcu_temp += ((amb + 12.0) - self.vcu_temp) * dt / 600.0
        self.vcu_temp = self._ov("vcu_temp", t, self.vcu_temp)
        self.cabin_temp += ((amb + 3.0) - self.cabin_temp) * dt / 900.0
        self.cabin_temp = self._ov("cabin_temp", t, self.cabin_temp)

    # -------------------------------------------------------------- 센서 출력 (VCU 입력)

    def inverter_status_frame(self, t: float) -> InverterFrame | None:
        if self.faults.inverter_can_blocked:
            return None
        omega = self.speed / self.p.wheel_radius_m * self.p.gear_ratio
        temp = self.inverter_temp + self.rng.gauss(0.0, self.p.temp_noise_c)
        return InverterFrame(t, temp, omega, self.applied_torque)

    def bms_frame(self, t: float) -> BmsFrame:
        v = self.battery_voltage + self.rng.gauss(0.0, self.p.voltage_noise_v)
        return BmsFrame(t, v, self.battery_current)

    def motor_temp_sensor_v(self) -> float:
        if self.faults.motor_temp_sensor_open:
            return TEMP_SENSOR_OPEN_V
        return temp_to_volt(self.motor_temp + self.rng.gauss(0.0, self.p.temp_noise_c))

    def wheel_speeds_kph(self) -> tuple[float, float]:
        base = self.speed * 3.6
        left = base + self.rng.gauss(0.0, self.p.wheel_noise_kph)
        right = base + self.rng.gauss(0.0, self.p.wheel_noise_kph)
        if self.faults.wheel_left_stuck:
            if self._left_frozen is None:
                self._left_frozen = left
            left = self._left_frozen
        else:
            self._left_frozen = None
        return left, right

    def vcu_board_temp(self) -> float:
        return self.vcu_temp + self.rng.gauss(0.0, self.p.temp_noise_c)
