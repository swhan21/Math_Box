"""명령행 진입점.

    python -m ucmssim list                       시나리오 목록
    python -m ucmssim run [--quick] [...]        고장 주입 시험 캠페인 (FI-01~13) → out/
    python -m ucmssim demo [--realtime 5 --live] 서술형 데모 (HMI 화면)
    python -m ucmssim gui [--port 8765]          웹 HMI (브라우저에서 http://127.0.0.1:8765/)
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import ConfigError, load_configs
from .harness.runner import resolve_ids, run_campaign
from .harness.scenarios import SCENARIOS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ucmssim", description="UGV VCU 대상 UCMS 개념검증 SIL 시뮬레이터")
    ap.add_argument("--config", type=Path, default=None, help="설정 디렉터리 (기본: ucms_sim/config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="시나리오 목록")

    run = sub.add_parser("run", help="고장 주입 시험 캠페인 실행")
    run.add_argument("--scenarios", default="all", help="예: all | FI-01,FI-08 | 1,8")
    run.add_argument("--reps", type=int, default=None, help="지연 측정 시나리오 반복 수 (기본: sim.yaml, 30)")
    run.add_argument("--fi11-duration", type=float, default=None, help="FI-11 정상 운전 시간 [s] (기본 3600)")
    run.add_argument("--quick", action="store_true", help="빠른 확인: 반복 3회, FI-11 300 s")
    run.add_argument("--seed", type=int, default=1)
    run.add_argument("--out", type=Path, default=Path("out"))

    demo = sub.add_parser("demo", help="서술형 데모")
    demo.add_argument("--realtime", type=float, default=None, help="배속 (1 = 실시간). 생략 시 최대 속도")
    demo.add_argument("--live", action="store_true", help="1초마다 HMI 화면 갱신 (--realtime 과 함께)")
    demo.add_argument("--db", type=Path, default=Path("out/demo.sqlite"))

    gui = sub.add_parser("gui", help="웹 HMI (실시간 시뮬레이션 + 브라우저 화면)")
    gui.add_argument("--host", default="127.0.0.1", help="바인딩 주소 (기본 127.0.0.1, 인증 없음 주의)")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--speed", type=float, default=1.0, help="시작 배속 (화면에서 변경 가능)")
    gui.add_argument("--db", type=Path, default=Path("out/gui.sqlite"))
    gui.add_argument("--open", action="store_true", help="브라우저 자동 열기")

    args = ap.parse_args(argv)
    try:
        cfg = load_configs(args.config)
    except ConfigError as e:
        ap.error(f"설정 오류: {e}")

    if args.cmd == "list":
        for sc in SCENARIOS.values():
            rep = " (반복)" if sc.repeated else ""
            print(f"{sc.id}  [{sc.level}] {sc.title} — {sc.expected}  {'/'.join(sc.criteria)}{rep}")
        return 0

    if args.cmd == "gui":
        from .gui.server import run_gui
        run_gui(cfg, args.host, args.port, args.speed, args.db, open_browser=args.open)
        return 0

    if args.cmd == "demo":
        from .demo import run_demo
        run_demo(cfg, args.db, realtime=args.realtime, live=args.live)
        return 0

    reps = 3 if args.quick and args.reps is None else args.reps
    fi11 = 300.0 if args.quick and args.fi11_duration is None else args.fi11_duration
    ids = resolve_ids(args.scenarios)
    print(f"UCMS SIL 시험 캠페인: {len(ids)}개 시나리오 → {args.out}/")
    _, summary = run_campaign(cfg, ids, args.out, reps=reps, seed=args.seed, fi11_duration_s=fi11)
    print()
    print(summary.split("## 지연 통계")[0].rstrip())
    print(f"\n상세: {args.out / 'summary.md'}, {args.out / 'results.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
