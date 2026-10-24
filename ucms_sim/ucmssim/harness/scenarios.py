"""고장 주입 시나리오 FI-01~FI-13 (설계안 8.2절).

각 시나리오는 새 시험대(Testbed)를 만들어 실행하고 RunResult 하나를 돌려준다.
t0 정의
  - 통신·신호·기동 수준 주입: 주입 명령 시각
  - 플랜트 프로파일 주입(FI-02·03·10): 프로파일이 검출 한계를 가로지르는 시각 (조건 성립 시각)
t1 = 하니스 구독자가 해당 이벤트를 수신한 시각 (같은 시계, 5.4절)
"""

from __future__ import annotations

import math
import statistics
from itertools import pairwise
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from ..config import Configs
from ..hmi import SimpleHmi
from ..topics import TOPICS, BitType, ExceedanceState, FaultEvent, Health, SpecProvider
from .profiles import crossing_time, drive_cycle, oscillation, piecewise, ramp_hold_return
from .testbed import Testbed


@dataclass
class RunResult:
    scenario_id: str
    run: int
    seed: int
    passed: bool = False
    detected: bool | None = None
    t_inject: float | None = None
    t0: float | None = None
    t1: float | None = None
    t_red: float | None = None
    event: str = ""
    db_recorded: bool | None = None
    ledger_missing: int = 0
    ledger_extra: int = 0
    note: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def latency_s(self) -> float | None:
        return None if self.t0 is None or self.t1 is None else self.t1 - self.t0

    @property
    def red_latency_s(self) -> float | None:
        return None if self.t0 is None or self.t_red is None else self.t_red - self.t0


@dataclass
class RunContext:
    cfg: Configs
    scenario_id: str
    run: int
    seed: int
    db_dir: Path
    fi11_duration_s: float

    def testbed(self, suffix: str = "", **kw) -> Testbed:
        return Testbed(self.cfg, self.seed, str(self.db_dir / f"{self.scenario_id}{suffix}.sqlite"), **kw)

    def result(self) -> RunResult:
        return RunResult(self.scenario_id, self.run, self.seed)


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    level: str
    expected: str
    criteria: tuple[str, ...]
    repeated: bool                      # 지연 측정 반복 대상 (8.3절)
    func: Callable[[RunContext], RunResult]


# ---------------------------------------------------------------- 공통 도우미


def _describe(ev: FaultEvent) -> str:
    latch = ",래치" if ev.latched else ""
    return f"{ev.event_id} {ev.fault_code_id} {ev.description} ({ev.severity},{ev.bit_type.value}{latch})"


def _record_hit(tb: Testbed, r: RunResult, hit, red=None) -> None:
    r.detected = hit is not None
    if hit is not None:
        r.t1, ev = hit
        r.event = _describe(ev)
        r.db_recorded = tb.ucms.store.has_event(ev.event_id)
    if red is not None:
        r.t_red = red[0]


def _checks(*items: tuple[str, Any]) -> str:
    return ", ".join(f"{name} {'OK' if ok else 'NG'}" for name, ok in items)


def _finish(tb: Testbed, r: RunResult) -> RunResult:
    tb.close()
    missing, extra = tb.ledger_diff()
    r.ledger_missing, r.ledger_extra = len(missing), len(extra)
    return r


def _random_phase(tb: Testbed) -> None:
    """어댑터 주기·샘플링 주기에 대한 주입 위상을 무작위화한다."""
    tb.run_for(tb.rng.uniform(0.0, 1.0))


# ---------------------------------------------------------------- FI-01 ~ FI-06: 고장 검출


def fi01(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up()
    _random_phase(tb)
    r.t_inject = r.t0 = tb.now
    tb.plant.faults.inverter_can_blocked = True
    hit = tb.wait_fault("F01", "ACTIVE", r.t0, 5.0)
    red = tb.wait_health_is(Health.RED, r.t0, 5.0)
    _record_hit(tb, r, hit, red)

    tb.plant.faults.inverter_can_blocked = False
    t_rec = tb.now
    cleared = tb.wait_fault("F01", "CLEARED", t_rec, 5.0)
    green = tb.wait_health_is(Health.GREEN, t_rec, 5.0)
    same_id = bool(hit and cleared and cleared[1].event_id == hit[1].event_id)
    r.passed = bool(hit and red and r.db_recorded and same_id and green)
    r.note = _checks(("RED", red), ("자동 해제", same_id), ("GREEN 복귀", green))
    return _finish(tb, r)


def fi02(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    bit = ctx.cfg.sim.bit
    c01 = ctx.cfg.vcu.characteristic("C01")
    tb.bring_up()
    _random_phase(tb)
    t = tb.now
    pts = ramp_hold_return(t, tb.plant.motor_temp, bit.motor_overtemp_set_c + 10.0, 4.0, 3.0, 80.0)
    tb.plant.set_override("motor_temp", piecewise(pts))
    r.t_inject, r.t0 = t, crossing_time(pts, bit.motor_overtemp_set_c)

    hit = tb.wait_fault("F02", "ACTIVE", t, pts[1][0] - t + 5.0)
    red = tb.wait_health_is(Health.RED, t, 2.0)
    _record_hit(tb, r, hit, red)
    te = tb.wait_threshold("C01", "EXCEEDED", t, 5.0)
    latched = tb.wait_fault("F02", "LATCHED", t, pts[-1][0] - tb.now + 5.0)
    released = tb.wait_threshold("C01", "RELEASED", t, 10.0)
    latest = tb.observer.latest_health(tb.vcu_id)
    red_while_latched = latest is not None and latest.health is Health.RED

    t_clr = tb.now
    tb.hmi.clear_latched(tb.vcu_id, "F02")                 # 운용자 해제 (O-role)
    cleared = tb.wait_fault("F02", "CLEARED", t_clr, 3.0)
    green = tb.wait_health_is(Health.GREEN, t_clr, 3.0)
    if te is not None:
        r.metrics["threshold_latency_s"] = te[0] - crossing_time(pts, c01.threshold.max)
    r.passed = bool(hit and red and r.db_recorded and te and latched and released
                    and red_while_latched and cleared and green)
    r.note = _checks(("C01 임계 초과", te), ("래치", latched), ("래치 중 RED 유지", red_while_latched),
                     ("C01 복귀", released), ("운용자 해제", cleared), ("GREEN 복귀", green))
    return _finish(tb, r)


def fi03(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    bit = ctx.cfg.sim.bit
    c02 = ctx.cfg.vcu.characteristic("C02")
    tb.bring_up()
    _random_phase(tb)
    t, v0 = tb.now, tb.plant.battery_voltage
    pts = ramp_hold_return(t, v0, bit.battery_uv_set_v - 20.0, 10.0, 3.0, v0)
    tb.plant.set_override("battery_voltage", piecewise(pts))
    r.t_inject, r.t0 = t, crossing_time(pts, bit.battery_uv_set_v)

    hit = tb.wait_fault("F03", "ACTIVE", t, pts[1][0] - t + 5.0)
    red = tb.wait_health_is(Health.RED, t, 2.0)
    _record_hit(tb, r, hit, red)
    te = tb.wait_threshold("C02", "EXCEEDED", t, 5.0)
    cleared = tb.wait_fault("F03", "CLEARED", t, pts[-1][0] - tb.now + 5.0)
    released = tb.wait_threshold("C02", "RELEASED", t, 10.0)
    green = tb.wait_health_is(Health.GREEN, t, 3.0) if cleared else None
    if te is not None:
        r.metrics["threshold_latency_s"] = te[0] - crossing_time(pts, c02.threshold.min)
    r.passed = bool(hit and red and r.db_recorded and te and cleared and released and green)
    r.note = _checks(("C02 하한 초과", te), ("자동 해제", cleared), ("C02 복귀", released), ("GREEN 복귀", green))
    return _finish(tb, r)


def fi04(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up()
    _random_phase(tb)
    r.t_inject = r.t0 = tb.now
    tb.plant.faults.motor_temp_sensor_open = True
    hit = tb.wait_fault("F04", "ACTIVE", r.t0, 3.0)
    _record_hit(tb, r, hit)
    tb.run_for(3.0)
    red = tb.observer.find_health(tb.vcu_id, r.t0, lambda mr: mr.health is Health.RED)
    c01_during = [t for t, _ in tb.observer.values.get((tb.vcu_id, "C01"), []) if t > r.t0 + 0.05]

    tb.plant.faults.motor_temp_sensor_open = False
    t_rec = tb.now
    cleared = tb.wait_fault("F04", "CLEARED", t_rec, 5.0)
    resumed = tb.run_until(
        lambda: any(t > t_rec for t, _ in tb.observer.values.get((tb.vcu_id, "C01"), [])), 3.0
    )
    r.passed = bool(hit and r.db_recorded and red is None and not c01_during and cleared and resumed)
    r.note = _checks(("심각도 medium → GREEN 유지", red is None), ("단선 중 C01 무효값 미발행", not c01_during),
                     ("자동 해제", cleared), ("C01 재개", resumed))
    return _finish(tb, r)


def fi05(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up(speed_kph=20.0)
    _random_phase(tb)
    r.t_inject = r.t0 = tb.now
    tb.plant.faults.wheel_left_stuck = True
    tb.vcu.set_speed(40.0)                                  # 가속 중 좌우 편차 발생
    hit = tb.wait_fault("F05", "ACTIVE", r.t0, 5.0)
    _record_hit(tb, r, hit)
    tb.plant.faults.wheel_left_stuck = False
    t_rec = tb.now
    cleared = tb.wait_fault("F05", "CLEARED", t_rec, 5.0)
    tb.vcu.set_speed(20.0)
    r.passed = bool(hit and r.db_recorded and cleared)
    r.note = _checks(("자동 해제", cleared))
    return _finish(tb, r)


def fi06(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up()
    tb.nvm.corrupt("calibration")
    r.t_inject = r.t0 = tb.now
    tb.vcu.reboot()
    hit = tb.wait_fault("F06", "ACTIVE", r.t0, 5.0)
    pbit_fail = tb.wait_health(
        r.t0, 5.0, lambda mr: mr.pbit_complete and not mr.pbit_passed and mr.health is Health.RED
    )
    _record_hit(tb, r, hit, pbit_fail)
    is_pbit = bool(hit and hit[1].bit_type is BitType.PBIT)

    tb.run_for(2.0)
    t2 = tb.now
    tb.vcu.reboot()                                         # 재기동 해제 (NVM은 기본값으로 복구됨)
    cleared = tb.wait_fault("F06", "CLEARED", t2, 5.0)
    pbit_ok = tb.wait_health(t2, 5.0, lambda mr: mr.pbit_complete and mr.pbit_passed and mr.health is Health.GREEN)
    r.metrics["boot_count"] = tb.vcu.boot_count
    r.passed = bool(hit and r.db_recorded and is_pbit and pbit_fail and cleared and pbit_ok)
    r.note = _checks(("BIT 유형 PBIT", is_pbit), ("PBIT 실패 보고·RED", pbit_fail),
                     ("재기동 후 해제", cleared), ("PBIT 통과·GREEN", pbit_ok))
    return _finish(tb, r)


# ---------------------------------------------------------------- FI-07: 히스테리시스


def fi07(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    limit = ctx.cfg.vcu.characteristic("C01").threshold.max
    amplitude, period, duration = 3.0, 3.0, 60.0
    tb.bring_up()
    t_s = tb.now
    tb.plant.set_override("motor_temp", oscillation(limit, amplitude, period, t_s, duration, 80.0, 4.0))
    r.t_inject = t_s
    tb.run_for(duration + (limit + amplitude - 80.0) / 4.0 + 5.0)

    exceeded = [ev for t, ev in tb.observer.threshold_events
                if t >= t_s and ev.characteristic_id == "C01" and ev.state is ExceedanceState.EXCEEDED]
    faults = [ev for t, ev in tb.observer.fault_events if t >= t_s]
    vals = [v for t, v in tb.observer.values.get((tb.vcu_id, "C01"), []) if t_s <= t <= t_s + duration]
    naive = sum(1 for a, b in pairwise([limit] + vals) if a <= limit < b)
    r.detected = None
    r.metrics.update(threshold_events=len(exceeded), naive_events=naive, samples=len(vals))
    r.passed = len(exceeded) <= 1 and not faults
    r.note = (f"진동 {limit:g}±{amplitude:g}°C/{period:g}s {duration:g}s: 임계 초과 이벤트 {len(exceeded)}건 "
              f"(히스테리시스 없는 단순 비교라면 {naive}건), 고장 이벤트 {len(faults)}건")
    return _finish(tb, r)


# ---------------------------------------------------------------- FI-08·09: 재기동·늦은 접속


def fi08(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up()
    _random_phase(tb)
    r.t_inject = tb.now
    tb.plant.faults.inverter_can_blocked = True
    hit = tb.wait_fault("F01", "ACTIVE", r.t_inject, 5.0)
    r.event = _describe(hit[1]) if hit else ""
    tb.run_for(1.0)

    tb.stop_ucms()
    tb.run_for(3.0)
    tb.start_new_ucms()
    r.t0 = tb.now                                           # 재기동 시각 → t1 = 상태 복원 시각
    restored = tb.wait_fault("F01", "ACTIVE", r.t0, 3.0)
    red = tb.wait_health_is(Health.RED, r.t0, 3.0)
    if restored and red:
        r.t1 = max(restored[0], red[0])
    same_id = bool(hit and restored and restored[1].event_id == hit[1].event_id)
    (rows,) = tb.ucms.store.conn.execute(
        "SELECT COUNT(*) FROM fault_event WHERE fault_code_id = 'F01'"
    ).fetchone()
    r.db_recorded = rows == 1
    r.detected = hit is not None

    tb.plant.faults.inverter_can_blocked = False
    t_rec = tb.now
    cleared = tb.wait_fault("F01", "CLEARED", t_rec, 5.0)
    cleared_same = bool(hit and cleared and cleared[1].event_id == hit[1].event_id)
    within = r.latency_s is not None and r.latency_s <= 2.0
    r.passed = bool(hit and within and same_id and r.db_recorded and cleared_same)
    r.note = _checks(("2 s 내 복원(고장·RED)", within), ("동일 이벤트 ID", same_id),
                     (f"DB 중복 없음({rows}행)", rows == 1), ("재기동 후 해제 연결", cleared_same))
    return _finish(tb, r)


def fi09(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up()
    r.t_inject = tb.now
    tb.plant.faults.inverter_can_blocked = True
    hit = tb.wait_fault("F01", "ACTIVE", r.t_inject, 5.0)
    r.event = _describe(hit[1]) if hit else ""
    r.detected = hit is not None
    tb.run_for(5.0)

    late = SimpleHmi(tb.sched, tb.domain, ctx.cfg.qos, name="HMI-LATE")
    r.t0 = tb.now
    late.start()

    def shows_state() -> bool:
        mr = late.resources.get(tb.vcu_id)
        return mr is not None and mr.health is Health.RED and any(
            ev.fault_code_id == "F01" for ev in late.active_faults.values()
        )

    if tb.run_until(shows_state, 2.0):
        r.t1 = tb.now
    r.metrics["hmi_screen"] = late.render()
    tb.plant.faults.inverter_can_blocked = False
    r.passed = r.latency_s is not None and r.latency_s <= 0.5
    r.note = _checks(("접속 0.5 s 내 RED·활성 고장 표시", r.passed))
    return _finish(tb, r)


# ---------------------------------------------------------------- FI-10: 브리지


def fi10(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    fb = ctx.cfg.bridge.fallbacks[0]
    tb.bring_up()
    adopted = tb.ucms.registry.attribute_spec(fb.resource, fb.characteristic)
    bridged = adopted is not None and adopted[1] is SpecProvider.BRIDGE
    _random_phase(tb)
    t, c0 = tb.now, tb.plant.cabin_temp
    pts = ramp_hold_return(t, c0, fb.threshold.max + 10.0, 2.0, 3.0, c0)
    tb.plant.set_override("cabin_temp", piecewise(pts))
    r.t_inject, r.t0 = t, crossing_time(pts, fb.threshold.max)

    hit = tb.wait_threshold(fb.characteristic, "EXCEEDED", t, pts[1][0] - t + 10.0)
    r.detected = hit is not None
    provider_ok = False
    if hit is not None:
        r.t1, ev = hit
        provider_ok = ev.spec_provider is SpecProvider.BRIDGE
        r.event = (f"{ev.event_id} {ev.resource_id}/{ev.characteristic_id} {ev.limit_type.value} "
                   f"{ev.limit_value:g} (값 {ev.trigger_value:.1f}, 사양 {ev.spec_provider.value})")
        r.db_recorded = tb.ucms.store.has_event(ev.event_id)
    released = tb.wait_threshold(fb.characteristic, "RELEASED", t, pts[-1][0] - tb.now + 10.0)
    r.passed = bool(bridged and hit and provider_ok and r.db_recorded and released)
    r.note = _checks(("대기 후 브리지 사양 채택", bridged), ("브리지 사양으로 이벤트 생성", provider_ok),
                     ("복귀", released))
    return _finish(tb, r)


# ---------------------------------------------------------------- FI-11: 정상 운전 오경보


def fi11(ctx: RunContext) -> RunResult:
    duration = ctx.fi11_duration_s
    tb, r = ctx.testbed(), ctx.result()
    tb.bring_up(speed_kph=0.0)
    t_s = tb.now
    for offset, kph in drive_cycle(tb.rng, duration):
        tb.sched.call_at(t_s + offset, tb.vcu.set_speed, kph)
    ext = {"motor_temp_max": -math.inf, "inverter_temp_max": -math.inf,
           "battery_v_min": math.inf, "speed_kph_max": 0.0}

    def sample() -> None:
        p = tb.plant
        ext["motor_temp_max"] = max(ext["motor_temp_max"], p.motor_temp)
        ext["inverter_temp_max"] = max(ext["inverter_temp_max"], p.inverter_temp)
        ext["battery_v_min"] = min(ext["battery_v_min"], p.battery_voltage)
        ext["speed_kph_max"] = max(ext["speed_kph_max"], p.speed * 3.6)

    tb.every(1.0, sample)
    tb.run_for(duration)

    faults = [ev for _, ev in tb.observer.fault_events]
    thresholds = [ev for _, ev in tb.observer.threshold_events]
    red = tb.observer.find_health(tb.vcu_id, 0.0, lambda mr: mr.health is Health.RED)
    adapter, channel = tb.vcu.adapter, tb.vcu.channel
    r.metrics.update(ext)
    r.metrics.update(false_faults=len(faults), false_thresholds=len(thresholds),
                     samples_received=tb.ucms.samples_received,
                     adapter_errors=adapter.errors, queue_overflow=channel.overflow)
    r.passed = not faults and not thresholds and red is None and adapter.errors == 0 and channel.overflow == 0
    r.note = (f"{duration / 60:g}분 정상 운전: 오경보 고장 {len(faults)}건·임계 {len(thresholds)}건, "
              f"특성값 {tb.ucms.samples_received}샘플 | 모터 최고 {ext['motor_temp_max']:.1f}°C, "
              f"인버터 최고 {ext['inverter_temp_max']:.1f}°C, 배터리 최저 {ext['battery_v_min']:.1f} V, "
              f"최고 속도 {ext['speed_kph_max']:.1f} km/h")
    return _finish(tb, r)


# ---------------------------------------------------------------- FI-12: 이종 구현 구독자


def fi12(ctx: RunContext) -> RunResult:
    tb, r = ctx.testbed(), ctx.result()
    counts: dict[str, int] = {}
    schema_ok: dict[str, bool] = {}
    vendor_b = tb.domain.participant("VENDOR-B-SUBSCRIBER", vendor="mock-B")
    for name, cls in TOPICS.items():
        expected = {f.name for f in fields(cls)}

        def on_data(sample, info, name=name, expected=expected) -> None:
            if sample is None:
                return
            counts[name] = counts.get(name, 0) + 1
            schema_ok[name] = schema_ok.get(name, True) and set(sample) == expected

        vendor_b.create_reader(name, ctx.cfg.qos.for_topic(name), on_data, raw=True)

    tb.bring_up()
    t = tb.now
    tb.plant.faults.inverter_can_blocked = True
    tb.wait_fault("F01", "ACTIVE", t, 5.0)
    tb.hmi.clear_latched(tb.vcu_id, "F01")                 # 명령 토픽 트래픽 (래치 아님 → VCU가 거부)
    tb.plant.faults.inverter_can_blocked = False
    tb.wait_fault("F01", "CLEARED", t, 5.0)
    fb = ctx.cfg.bridge.fallbacks[0]
    pts = ramp_hold_return(tb.now, tb.plant.cabin_temp, fb.threshold.max + 10.0, 5.0, 3.0, tb.plant.cabin_temp)
    tb.plant.set_override("cabin_temp", piecewise(pts))
    tb.wait_threshold(fb.characteristic, "RELEASED", t, pts[-1][0] - tb.now + 10.0)
    tb.run_for(2.0)

    missing = sorted(n for n in TOPICS if not counts.get(n))
    bad_schema = sorted(n for n, ok in schema_ok.items() if not ok)
    incompatible = list(tb.domain.incompatible)
    r.detected = None
    r.metrics.update(topic_counts=counts, missing=missing, bad_schema=bad_schema, incompatible=len(incompatible))
    r.passed = not missing and not bad_schema and not incompatible
    r.note = (f"[모의 수준] 이종 구독자(mock-B, 타입 미사용 원시 해석) "
              f"수신 {len(TOPICS) - len(missing)}/{len(TOPICS)} 토픽, "
              f"스키마 불일치 {len(bad_schema)}, QoS 비호환 {len(incompatible)}"
              + (f" | 미수신 {missing}" if missing else "")
              + " — 실제 이종 DDS 상호운용은 T4(HILS)에서 확인 필요")
    return _finish(tb, r)


# ---------------------------------------------------------------- FI-13: 제어 영향 (SIL 대리 지표)


def fi13(ctx: RunContext) -> RunResult:
    r = ctx.result()
    duration = 60.0
    variants = (
        ("A", "기준(UCM 연동 없음)", dict(adapter_enabled=False, ucm_calls=False)),
        ("B", "UCM 연동", dict(adapter_enabled=True, ucm_calls=True)),
        ("C", "어댑터 정지(장애 모사)", dict(adapter_enabled=False, ucm_calls=True)),
    )
    out: dict[str, dict[str, Any]] = {}
    tb_b: Testbed | None = None
    for tag, label, kw in variants:
        tb = ctx.testbed(f"_{tag}", profile_control=True, **kw)
        tb.start()
        tb.run_until(lambda tb=tb: tb.vcu.pbit_result is not None, 5.0)
        t_s = tb.now
        for k in range(int(duration // 8.0)):               # 최대 부하: 0 ↔ 60 km/h 반복 가감속
            tb.sched.call_at(t_s + 8.0 * k, tb.vcu.set_speed, 60.0 if k % 2 == 0 else 0.0)
        tb.sched.call_at(t_s + 30.0, setattr, tb.plant, "estop_pressed", True)
        tb.sched.call_at(t_s + 32.0, setattr, tb.plant, "estop_pressed", False)
        tb.run_for(duration)
        ctrl = tb.vcu.control
        us = sorted(x * 1e6 for x in ctrl.step_wall_s)
        out[tag] = {
            "label": label,
            "digest": ctrl.trace_digest(),
            "steps": len(us),
            "mean_us": statistics.fmean(us),
            "p99_us": us[min(len(us) - 1, math.ceil(0.99 * len(us)) - 1)],
            "queue_pending": tb.vcu.channel.pending if tb.vcu.channel else 0,
            "queue_overflow": tb.vcu.channel.overflow if tb.vcu.channel else 0,
        }
        if tag == "B":
            tb_b = tb
            u02 = tb.ucms.usage.latest.get((tb.vcu_id, "U02"))
            out[tag]["estop_count_recorded"] = u02.value if u02 else None
            out[tag]["fault_events"] = len(tb.observer.fault_events)
            out[tag]["threshold_events"] = len(tb.observer.threshold_events)
        else:
            tb.close()

    identical = len({v["digest"] for v in out.values()}) == 1
    overhead = out["B"]["mean_us"] - out["A"]["mean_us"]
    r.detected = None
    r.metrics.update(variants=out, control_overhead_mean_us=overhead)
    r.passed = identical
    r.note = (f"제어 출력(토크 지령열) A=B=C 동일: {'OK' if identical else 'NG'} | "
              f"제어 1주기 실행시간 평균 A {out['A']['mean_us']:.1f} µs, B {out['B']['mean_us']:.1f} µs "
              f"(UCM 호출 부담 {overhead:+.1f} µs), "
              f"C 미처리 진단 기록 {out['C']['queue_pending']}건 누적에도 제어 무영향 | "
              f"U02 비상정지 횟수 기록 {out['B'].get('estop_count_recorded')} | "
              "Python SIL 실행시간은 타깃 지터와 무관(참고값), 지터는 T4에서 측정")
    return _finish(tb_b, r)


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in (
        Scenario("FI-01", "인버터 CAN 메시지 차단", "통신", "F01 활성 → Fault_Event → Red", ("SC2", "SC3"), True, fi01),
        Scenario("FI-02", "모터 온도 상승 프로파일", "플랜트", "C01 임계 초과 → F02 래치", ("SC2", "SC3"), True, fi02),
        Scenario("FI-03", "배터리 전압 강하", "플랜트", "C02 하한 → F03", ("SC2", "SC3"), True, fi03),
        Scenario("FI-04", "온도센서 단선", "신호", "F04 활성", ("SC2",), False, fi04),
        Scenario("FI-05", "휠속도 센서 고착", "신호", "F05 활성", ("SC2",), False, fi05),
        Scenario("FI-06", "메모리 CRC 오류 후 재기동", "기동", "F06 + PBIT 실패 보고", ("SC2",), False, fi06),
        Scenario("FI-07", "임계값 근처 진동", "플랜트", "히스테리시스 동작, 이벤트 난발 없음", ("SC4",), False, fi07),
        Scenario("FI-08", "고장 활성 중 UCMS 재기동", "서비스", "재기동 후 현재 상태 복원", ("SC6",), False, fi08),
        Scenario("FI-09", "고장 활성 후 HMI 늦은 접속", "서비스", "접속 즉시 현재 상태 표시", ("SC6",), False, fi09),
        Scenario("FI-10", "단순 장비 임계 초과", "브리지", "브리지 사양으로 이벤트 생성", ("SC2", "SC7"), False, fi10),
        Scenario("FI-11", "정상 운전 1시간", "—", "오경보 0건", ("SC4",), False, fi11),
        Scenario("FI-12", "이종 DDS 구독자 접속", "통신", "전 토픽 수신", ("SC9",), False, fi12),
        Scenario("FI-13", "최대 부하 운전", "—", "제어 주기 영향 측정", ("SC8",), False, fi13),
    )
}
