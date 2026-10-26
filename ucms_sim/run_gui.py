"""HMI 화면 실행. VS Code에서 이 파일을 열고 오른쪽 위 ▶(Run Python File)를 누른다.

명령행에서는 `python -m ucmssim gui --speed 5` 와 같다.
"""

from pathlib import Path

from ucmssim.__main__ import main

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    raise SystemExit(main(["gui", "--speed", "5", "--db", str(HERE / "out" / "gui.sqlite")]))
