"""시험 캠페인 실행: 시나리오 선택·반복 → 결과 수집 → 판정·파일 출력."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

from ..config import Configs
from .report import write_outputs
from .scenarios import SCENARIOS, RunContext, RunResult


def resolve_ids(spec: str) -> list[str]:
    if spec.strip().lower() == "all":
        return list(SCENARIOS)
    ids = []
    for token in spec.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if not token.startswith("FI-"):
            token = f"FI-{int(token):02d}"
        if token not in SCENARIOS:
            raise SystemExit(f"알 수 없는 시나리오: {token} (사용 가능: {', '.join(SCENARIOS)})")
        ids.append(token)
    return ids


def run_campaign(
    cfg: Configs,
    scenario_ids: Sequence[str],
    out_dir: Path,
    *,
    reps: int | None = None,
    seed: int = 1,
    fi11_duration_s: float | None = None,
    progress: Callable[[str], None] = print,
) -> tuple[list[RunResult], str]:
    reps = cfg.sim.harness.repetitions if reps is None else reps
    fi11 = cfg.sim.harness.fi11_duration_s if fi11_duration_s is None else fi11_duration_s
    db_dir = out_dir / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now().isoformat(timespec="seconds")
    wall0 = time.perf_counter()

    results: list[RunResult] = []
    for idx, sid in enumerate(scenario_ids):
        sc = SCENARIOS[sid]
        n = reps if sc.repeated else 1
        w0 = time.perf_counter()
        passed = 0
        for run in range(n):
            ctx = RunContext(cfg, sid, run, seed * 100_000 + idx * 1_000 + run, db_dir, fi11)
            r = sc.func(ctx)
            results.append(r)
            passed += r.passed
        last = results[-1]
        lat = f", 지연 {last.latency_s:.3f} s" if last.latency_s is not None else ""
        progress(f"  {sid} {sc.title:<24} {passed}/{n} 통과{lat}  ({time.perf_counter() - w0:.1f} s)")

    meta = {
        "started": started,
        "elapsed_s": time.perf_counter() - wall0,
        "seed": seed,
        "reps": reps,
        "fi11_duration_s": fi11,
    }
    summary = write_outputs(out_dir, cfg, results, meta)
    return results, summary
