"""HMI 웹 서버 (표준 라이브러리 http.server).

    GET  /            HMI 화면 (static/index.html)
    GET  /api/state   HMI 상태 + 시험 하니스 상태 (JSON)
    POST /api/cmd     {"action": "...", "args": {...}}  — Content-Type: application/json 필수

인증이 없으므로 기본은 127.0.0.1 에만 바인딩한다. JSON 본문만 받으므로 다른 사이트의 페이지가
브라우저를 통해 명령을 보내려 하면 CORS 사전 요청에서 막힌다.
"""

from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Configs
from .runtime import CommandError, SimRuntime

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 4096


def make_handler(runtime: SimRuntime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ucmssim-hmi"

        def log_message(self, fmt: str, *args) -> None:  # 요청 로그 생략
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, runtime.snapshot())
            else:
                self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/api/cmd":
                self._json(404, {"ok": False, "error": "not found"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"ok": False, "error": "Content-Type: application/json 필요"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"ok": False, "error": "요청이 너무 큼"})
                return
            try:
                req = json.loads(self.rfile.read(length) or b"{}")
                action, args = req["action"], req.get("args") or {}
                if not isinstance(action, str) or not isinstance(args, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self._json(400, {"ok": False, "error": "형식: {\"action\": str, \"args\": object}"})
                return
            try:
                self._json(200, {"ok": True, "message": runtime.command(action, args)})
            except CommandError as e:
                self._json(400, {"ok": False, "error": str(e)})

    return Handler


def make_server(runtime: SimRuntime, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(runtime))
    server.daemon_threads = True
    return server


def run_gui(cfg: Configs, host: str, port: int, speed: float, db_path: Path, open_browser: bool = False) -> None:
    runtime = SimRuntime(cfg, db_path, speed=speed)
    server = make_server(runtime, host, port)
    runtime.start()
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"UCMS 간이 HMI: {url}   (배속 {speed:g}×, DB {db_path}, 종료 Ctrl+C)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n종료")
    finally:
        server.server_close()
        runtime.stop()
