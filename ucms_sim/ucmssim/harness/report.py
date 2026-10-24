"""성공 기준(SC1~SC9) 판정과 결과 파일 생성 (설계안 2.2절, 8.3절).

출력: results.csv (시나리오 실행별), summary.md (판정 요약), deviation_log.md, hmi/*.txt
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .. import deviation
from ..config import Configs
from ..vcu.control import DTC_TABLE
from .scenarios import SCENARIOS, RunResult

MET, NOT_MET, PENDING, NOT_RUN = "충족", "미충족", "보류", "미실행"


@dataclass(frozen=True)
class Verdict:
    id: str
    metric: str
    target: str
    result: str
    verdict: str


def percentile(values: Sequence[float], p: float) -> float:
    """최근접 순위(nearest-rank) 백분위수."""
    xs = sorted(values)
    return xs[max(0, math.ceil(p / 100.0 * len(xs)) - 1)]


def _runs(results: Sequence[RunResult], *ids: str) -> list[RunResult]:
    return [r for r in results if r.scenario_id in ids]


def latency_stats(results: Sequence[RunResult], scenario_id: str, p: float) -> dict | None:
    runs = _runs(results, scenario_id)
    if not runs:
        return None
    lat = [r.latency_s if r.latency_s is not None else math.inf for r in runs]
    return {
        "n": len(lat),
        "p50": percentile(lat, 50),
        "p95": percentile(lat, p),
        "max": max(lat),
        "min": min(lat),
    }


def sc1_mapping(cfg: Configs) -> tuple[str, str, list[tuple[str, str, str]]]:
    """기존 VCU 진단 목록 대비 UCM 매핑률."""
    mapped = {f.internal_dtc: f for f in cfg.vcu.faults}
    unmapped = {u.internal_dtc: u.reason for u in cfg.vcu.unmapped}
    rows, undocumented = [], []
    for dtc, desc in sorted(DTC_TABLE.items()):
        if dtc in mapped:
            rows.append((f"0x{dtc:04X}", desc, f"→ {mapped[dtc].id}"))
        elif dtc in unmapped:
            rows.append((f"0x{dtc:04X}", desc, f"매핑 제외: {unmapped[dtc]}"))
        else:
            rows.append((f"0x{dtc:04X}", desc, "미매핑 (사유 없음)"))
            undocumented.append(dtc)
    unknown = [d for d in mapped if d not in DTC_TABLE]
    selected = len(DTC_TABLE) - len(unmapped)
    rate = sum(1 for d in DTC_TABLE if d in mapped) / selected if selected else 0.0
    ok = not undocumented and not unknown and rate == 1.0
    result = f"선정 {selected}항목 중 {rate * 100:.0f}% 매핑, 제외 {len(unmapped)}항목 사유 기록"
    if unknown:
        result += f", 진단 목록에 없는 DTC {[hex(d) for d in unknown]}"
    return result, MET if ok else NOT_MET, rows


def evaluate(cfg: Configs, results: Sequence[RunResult]) -> list[Verdict]:
    h = cfg.sim.harness
    ran = {r.scenario_id for r in results}
    out: list[Verdict] = []

    sc1_result, sc1_verdict, _ = sc1_mapping(cfg)
    out.append(Verdict("SC1", "기존 VCU 진단 항목의 UCM 매핑률", "선정 항목 100%", sc1_result, sc1_verdict))

    sc2_ids = ("FI-01", "FI-02", "FI-03", "FI-04", "FI-05", "FI-06", "FI-10")
    sc2_runs = _runs(results, *sc2_ids)
    if sc2_runs:
        detected = sum(1 for r in sc2_runs if r.detected)
        missing_ids = sorted(set(sc2_ids) - ran)
        kinds = sum(1 for sid in sc2_ids if sid in ran and all(r.detected for r in _runs(results, sid)))
        res = f"{kinds}/{len(sc2_ids)}종 탐지 (실행 {detected}/{len(sc2_runs)})"
        if missing_ids:
            res += f" (미실행 {', '.join(missing_ids)})"
        if detected < len(sc2_runs):
            verdict = NOT_MET
        else:
            verdict = PENDING if missing_ids else MET
        out.append(Verdict("SC2", "고장 시나리오 탐지율", "7종 전부 탐지", res, verdict))
    else:
        out.append(Verdict("SC2", "고장 시나리오 탐지율", "7종 전부 탐지", "-", NOT_RUN))

    lat_runs = _runs(results, "FI-01", "FI-02", "FI-03")
    if lat_runs:
        lat = [r.latency_s if r.latency_s is not None else math.inf for r in lat_runs]
        p95 = percentile(lat, h.latency_percentile)
        res = f"p{h.latency_percentile:g} = {p95:.3f} s (n={len(lat)}, 최대 {max(lat):.3f} s)"
        target = f"p{h.latency_percentile:g} ≤ {h.latency_limit_s:g} s"
        out.append(Verdict("SC3", "고장 주입 → Fault_Event 수신 지연", target, res,
                           MET if p95 <= h.latency_limit_s else NOT_MET))
    else:
        out.append(Verdict("SC3", "고장 주입 → Fault_Event 수신 지연", f"p95 ≤ {h.latency_limit_s:g} s", "-", NOT_RUN))

    sc4 = _runs(results, "FI-07", "FI-11")
    if sc4:
        fi11 = _runs(results, "FI-11")
        parts = [f"{r.scenario_id}: {'통과' if r.passed else '실패'}" for r in sc4]
        if fi11:
            m = fi11[0].metrics
            parts.append(f"FI-11 오경보 {m.get('false_faults', 0) + m.get('false_thresholds', 0)}건")
        verdict = MET if all(r.passed for r in sc4) and {"FI-07", "FI-11"} <= ran else (
            NOT_MET if not all(r.passed for r in sc4) else PENDING)
        out.append(Verdict("SC4", "정상 운전 1시간 오경보", "0건", ", ".join(parts), verdict))
    else:
        out.append(Verdict("SC4", "정상 운전 1시간 오경보", "0건", "-", NOT_RUN))

    missing = sum(r.ledger_missing for r in results)
    extra = sum(r.ledger_extra for r in results)
    out.append(Verdict("SC5", "이벤트 저장 누락", "0건 (DB 기록 = 발행 이벤트)",
                       f"누락 {missing}건, DB에만 있음 {extra}건 ({len(results)}회 실행 합계)",
                       MET if results and missing == 0 and extra == 0 else (NOT_RUN if not results else NOT_MET)))

    out.append(_simple(results, ran, "SC6", "재시작·늦은 접속 시 현재 상태 복원", "100%", ("FI-08", "FI-09")))
    out.append(_simple(results, ran, "SC7", "브리지 사양으로 단순 장비 고장 이벤트 생성", "1종 이상", ("FI-10",)))

    fi13 = _runs(results, "FI-13")
    if fi13:
        m = fi13[0].metrics
        res = (f"제어 출력 동일성 {'확인' if fi13[0].passed else '불일치'}, "
               f"UCM 호출 부담 평균 {m.get('control_overhead_mean_us', float('nan')):+.1f} µs (Python SIL 참고값)")
        out.append(Verdict("SC8", "VCU 제어 주기 지터 증가", "무시 가능 (기준값 TBD)", res,
                           PENDING if fi13[0].passed else NOT_MET))
    else:
        out.append(Verdict("SC8", "VCU 제어 주기 지터 증가", "무시 가능 (기준값 TBD)", "-", NOT_RUN))

    out.append(_simple(results, ran, "SC9", "이종 DDS 구독자의 토픽 수신", "전 토픽 수신 (SIL은 모의 수준)",
                       ("FI-12",)))
    return out


def _simple(results, ran, sc_id, metric, target, ids) -> Verdict:
    runs = _runs(results, *ids)
    if not runs:
        return Verdict(sc_id, metric, target, "-", NOT_RUN)
    passed = sum(1 for r in runs if r.passed)
    res = ", ".join(f"{r.scenario_id}: {'통과' if r.passed else '실패'}" for r in runs)
    if passed < len(runs):
        verdict = NOT_MET
    elif set(ids) <= ran:
        verdict = MET
    else:
        verdict = PENDING
    return Verdict(sc_id, metric, target, res, verdict)


# ---------------------------------------------------------------- 파일 출력

CSV_FIELDS = (
    "scenario_id", "run", "seed", "passed", "detected", "t_inject", "t0", "t1", "latency_s",
    "t_red", "red_latency_s", "event", "db_recorded", "ledger_missing", "ledger_extra", "note",
)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def write_csv(path: Path, results: Sequence[RunResult]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_FIELDS)
        for r in results:
            w.writerow([_fmt(getattr(r, k)) for k in CSV_FIELDS])


def summary_markdown(cfg: Configs, results: Sequence[RunResult], meta: dict) -> str:
    h = cfg.sim.harness
    verdicts = evaluate(cfg, results)
    lines = [
        "# UCMS SIL 시뮬레이션 결과",
        "",
        f"- 실행: {meta.get('started', '')}, 소요 {meta.get('elapsed_s', 0):.1f} s (벽시계)",
        f"- 시드 {meta.get('seed')}, 지연 측정 반복 {meta.get('reps')}회, FI-11 {meta.get('fi11_duration_s', 0):g} s",
        f"- 설정: `{cfg.source_dir}`",
        "- 환경: SIL (8.1절 T2) — 모의 DDS, 모의 시계. T3·T4(HILS)에서 재확인 필요",
        "",
        "## 성공 기준 판정",
        "",
        "| ID | 지표 | 목표 | 결과 | 판정 |",
        "|---|---|---|---|---|",
    ]
    for v in verdicts:
        lines.append(f"| {v.id} | {v.metric} | {v.target} | {v.result} | **{v.verdict}** |")

    lines += ["", "## 지연 통계 (SC3, t0 → Fault_Event 수신)", "",
              "| 시나리오 | t0 정의 | n | 최소 | p50 | p95 | 최대 |", "|---|---|---|---|---|---|---|"]
    t0_def = {"FI-01": "CAN 차단 시각", "FI-02": "모터 온도가 BIT 한계를 넘는 시각",
              "FI-03": "배터리 전압이 BIT 한계 아래로 내려가는 시각"}
    for sid in ("FI-01", "FI-02", "FI-03"):
        s = latency_stats(results, sid, h.latency_percentile)
        if s:
            lines.append(f"| {sid} | {t0_def[sid]} | {s['n']} | {s['min']:.3f} | {s['p50']:.3f} | "
                         f"{s['p95']:.3f} | {s['max']:.3f} |")
    lines += ["",
              "지연 구성: BIT 검출 시간(CAN 타임아웃 0.1 s, 과온·저전압 디바운스 0.5 s) + 어댑터 주기(≤0.1 s)"
              " + DDS 전송(0.5~3 ms ×2) + UCMS 처리(5 ms).", ""]

    lines += ["## 시나리오별 결과", "", "| ID | 시나리오 | 기대 결과 | 실행 | 통과 | 비고 (마지막 실행) |",
              "|---|---|---|---|---|---|"]
    for sid, sc in SCENARIOS.items():
        runs = _runs(results, sid)
        if not runs:
            continue
        passed = sum(1 for r in runs if r.passed)
        note = runs[-1].note.replace("|", "/")
        lines.append(f"| {sid} | {sc.title} | {sc.expected} | {len(runs)} | {passed}/{len(runs)} | {note} |")

    _, _, rows = sc1_mapping(cfg)
    lines += ["", "## 진단 항목 매핑 (SC1)", "", "| 내부 DTC | 기존 진단 | UCM 매핑 |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    lines += ["", "## 산출물", "",
              "- `results.csv`: 실행별 t0·t1·지연·이벤트·DB 기록 여부 (8.3절)",
              "- `db/*.sqlite`: 시나리오별 UCMS 저장소 (마지막 실행)",
              "- `deviation_log.md`: 편차 기록부",
              "- `hmi/*.txt`: 간이 HMI 화면 스냅샷", ""]
    return "\n".join(lines)


def write_outputs(out_dir: Path, cfg: Configs, results: Sequence[RunResult], meta: dict) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "results.csv", results)
    summary = summary_markdown(cfg, results, meta)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    (out_dir / "deviation_log.md").write_text(deviation.to_markdown(), encoding="utf-8")
    hmi_dir = out_dir / "hmi"
    for r in results:
        screen = r.metrics.get("hmi_screen")
        if screen:
            hmi_dir.mkdir(exist_ok=True)
            (hmi_dir / f"{r.scenario_id}_run{r.run}.txt").write_text(screen + "\n", encoding="utf-8")
    return summary
