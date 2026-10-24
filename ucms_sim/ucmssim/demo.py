"""서술형 데모: 기동 → FI-01 → 복구 → FI-02(래치·운용자 해제) → FI-10(브리지) 를 HMI 화면으로 보여준다."""

from __future__ import annotations

import sys
from pathlib import Path

from .config import Configs
from .harness.profiles import piecewise, ramp_hold_return
from .harness.testbed import Testbed
from .topics import Health
from .ucms.persistence import dump_summary


def run_demo(cfg: Configs, db_path: Path, realtime: float | None = None, live: bool = False) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tb = Testbed(cfg, seed=7, db_path=str(db_path), realtime=realtime)
    vcu = tb.vcu_id
    if live:
        tb.every(1.0, lambda: print("\033[2J\033[H" + tb.hmi.render(), flush=True))

    def show(title: str) -> None:
        if not live:
            print(f"\n### {title}\n{tb.hmi.render()}")

    print("1) 기동: VCU 부팅·PBIT, 사양 발행, 브리지 대기 5 s, 20 km/h 주행")
    tb.bring_up(speed_kph=20.0)
    show("정상 운전")

    print("2) FI-01 인버터 CAN 메시지 차단")
    t0 = tb.now
    tb.plant.faults.inverter_can_blocked = True
    hit = tb.wait_fault("F01", "ACTIVE", t0, 5.0)
    tb.run_for(1.0)
    if hit:
        print(f"   Fault_Event 수신 지연 {hit[0] - t0:.3f} s")
    show("FI-01 고장 활성")

    print("3) CAN 복구 → 자동 해제")
    tb.plant.faults.inverter_can_blocked = False
    tb.wait_health_is(Health.GREEN, tb.now, 5.0)
    tb.run_for(1.0)
    show("복구")

    print("4) FI-02 모터 과온 프로파일 → F02 래치 → 운용자 해제")
    t = tb.now
    pts = ramp_hold_return(t, tb.plant.motor_temp, 160.0, 4.0, 3.0, 80.0)
    tb.plant.set_override("motor_temp", piecewise(pts))
    tb.wait_fault("F02", "ACTIVE", t, 60.0)
    tb.run_for(2.0)
    show("F02 활성 (과온 중)")
    tb.wait_fault("F02", "LATCHED", t, 60.0)
    tb.wait_threshold("C01", "RELEASED", t, 10.0)
    show("온도 정상화 후에도 F02 래치 유지")
    tb.hmi.clear_latched(vcu, "F02", operator="정비요원")
    tb.wait_fault("F02", "CLEARED", tb.now, 3.0)
    tb.run_for(1.0)
    show("운용자 해제 후")

    print("5) FI-10 단순 장비(R-role 미지원) 실내온도 상승 → 브리지 사양으로 판정")
    t = tb.now
    c0 = tb.plant.cabin_temp
    pts = ramp_hold_return(t, c0, 60.0, 2.0, 3.0, c0)
    tb.plant.set_override("cabin_temp", piecewise(pts))
    tb.wait_threshold("cabin_temp", "EXCEEDED", t, 40.0)
    tb.run_for(1.0)
    show("브리지 임계 초과")
    tb.wait_threshold("cabin_temp", "RELEASED", t, 60.0)

    tb.close()
    print(f"\n6) UCMS 저장소 요약 ({db_path})")
    for table, n in dump_summary(str(db_path)).items():
        print(f"   {table:<22} {n:>6} 행")
    sys.stdout.flush()
