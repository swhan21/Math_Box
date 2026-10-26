"""고장 주입 시험 캠페인 빠른 확인. VS Code에서 이 파일을 열고 ▶(Run Python File)를 누른다.

결과는 이 폴더의 out/ (summary.md, results.csv). 전체 시험은 `python -m ucmssim run`.
"""

from pathlib import Path

from ucmssim.__main__ import main

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    raise SystemExit(main(["run", "--quick", "--out", str(HERE / "out")]))
