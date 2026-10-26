"""HMI 화면 구성 요소: 스크롤 영역, 카드, 상태 칩, 특성값 추이 차트, 표시 형식."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import Any

from .theme import Theme, blend

UNITS = {"degC": "°C", "V": "V", "s": "s", "count": "회"}
SEVERITY = {"high": ("critical", "높음"), "medium": ("warning", "중간"), "low": ("muted", "낮음")}
EVENT_STATUS = {"fault": "critical", "health_red": "critical", "exceeded": "critical", "latched": "serious",
                "cleared": "good", "health_green": "good", "released": "good", "command": "neutral"}
USAGE_LABELS = {"powered_on_time": "누적 가동시간", "emergency_stop_count": "비상정지 작동"}


def unit(u: str | None) -> str:
    return UNITS.get(u or "", u or "")


def fmt_t(t: float) -> str:
    t = max(0.0, t or 0.0)
    return f"T+{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}"


def fmt_dur(sec: float) -> str:
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{int(sec % 60):02d}"


def fmt_v(v: float | None, digits: int = 1) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


class ScrollFrame(tk.Frame):
    """세로 스크롤 영역. 내용은 self.inner 에 넣는다."""

    def __init__(self, master: tk.Misc, th: Theme, width: int | None = None) -> None:
        bg = th.c["page"]
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, width=width or 10)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _bind_wheel(self, _e: Any) -> None:
        self.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-2 if e.delta > 0 else 2, "units"))
        self.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

    def _unbind_wheel(self, _e: Any) -> None:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.unbind_all(seq)


def card(master: tk.Misc, th: Theme, title: str, subtitle: str = "") -> tuple[tk.Frame, tk.Frame, tk.Frame]:
    """(카드, 머리줄, 본문). 머리줄 오른쪽에 버튼 등을 더 붙일 수 있다."""
    c = th.c
    frame = tk.Frame(master, bg=c["surface"], highlightbackground=c["border"], highlightthickness=1)
    head = tk.Frame(frame, bg=c["surface"])
    head.pack(fill="x", padx=16, pady=(12, 8))
    tk.Label(head, text=title, bg=c["surface"], fg=c["text"], font=th.font("title")).pack(side="left")
    if subtitle:
        tk.Label(head, text=subtitle, bg=c["surface"], fg=c["text2"], font=th.font("small")).pack(
            side="left", padx=(8, 0))
    body = tk.Frame(frame, bg=c["surface"])
    body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
    return frame, head, body


class Chip(tk.Label):
    """상태 칩: 색 점 + 글자 (색만으로 의미를 전하지 않는다)."""

    def __init__(self, master: tk.Misc, th: Theme, bg: str) -> None:
        super().__init__(master, bg=bg, fg=th.c["text"], font=th.font("small"), compound="left", padx=2)
        self.th = th
        self._state: tuple[str, str] | None = None

    def set(self, status: str, text: str) -> None:
        if self._state != (status, text):
            self._state = (status, text)
            self.configure(image=self.th.dot(status), text=f" {text}")


def nice_ticks(lo: float, hi: float, count: int = 4) -> tuple[list[float], float]:
    raw = (hi - lo) / count
    mag = 10 ** math.floor(math.log10(raw))
    n = raw / mag
    step = (1 if n < 1.5 else 2 if n < 3 else 5 if n < 7 else 10) * mag
    ticks, v = [], math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        ticks.append(round(v, 6))
        v += step
    return ticks, step


class TrendChart:
    """특성값 1개의 최근 추이. 점선 = 임계값, 넘는 쪽은 옅게 칠한다. 커서를 올리면 값을 보여준다."""

    WINDOW_S = 180.0
    HEIGHT = 150
    X_TICKS = ((-180, "-3분", "sw"), (-120, "-2분", "s"), (-60, "-1분", "s"), (0, "현재", "se"))

    def __init__(self, master: tk.Misc, th: Theme) -> None:
        self.th = th
        c, bg = th.c, th.c["surface2"]
        self.frame = tk.Frame(master, bg=bg, highlightbackground=c["border"], highlightthickness=1)
        head = tk.Frame(self.frame, bg=bg)
        head.pack(fill="x", padx=12, pady=(10, 0))
        names = tk.Frame(head, bg=bg)
        names.pack(side="left", fill="x", expand=True)
        self.title = tk.Label(names, bg=bg, fg=c["text"], font=th.font("title"), anchor="w")
        self.title.pack(fill="x")
        self.subtitle = tk.Label(names, bg=bg, fg=c["text2"], font=th.font("mono"), anchor="w")
        self.subtitle.pack(fill="x")
        self.unit = tk.Label(head, bg=bg, fg=c["text2"], font=th.font("small"))
        self.unit.pack(side="right", anchor="n", pady=(6, 0))
        self.value = tk.Label(head, bg=bg, fg=c["text"], font=th.font("value"))
        self.value.pack(side="right", anchor="n")
        self.chip = Chip(self.frame, th, bg)
        self.chip.pack(anchor="w", padx=12, pady=(4, 0))
        self.canvas = tk.Canvas(self.frame, height=self.HEIGHT, bg=bg, highlightthickness=0)
        self.canvas.pack(fill="x", padx=4, pady=(2, 6))
        self.canvas.bind("<Configure>", lambda e: self.draw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.tr: dict[str, Any] | None = None
        self.now = 0.0
        self.exceeded = False
        self.hover_x: float | None = None
        self._geom: tuple | None = None

    def set(self, tr: dict[str, Any], now: float, exceeded: bool) -> None:
        self.tr, self.now, self.exceeded = tr, now, exceeded
        last = tr["points"][-1][1] if tr["points"] else None
        same = tr["name"] == tr["characteristic_id"]
        self.title.configure(text=tr["name"] if same else f"{tr['characteristic_id']} · {tr['name']}")
        self.subtitle.configure(text=tr["resource_id"] + ("  · 브리지 사양" if tr["provider"] == "BRIDGE" else ""))
        self.value.configure(text=fmt_v(last))
        self.unit.configure(text=f" {unit(tr['unit'])}")
        if last is None:
            self.chip.set("muted", "값 없음")
        elif exceeded:
            self.chip.set("critical", "임계 초과")
        elif tr["stale"]:
            self.chip.set("muted", "갱신 지연")
        else:
            self.chip.set("good", "정상 범위")
        self.draw()

    # -------------------------------------------------------------- 그리기

    def draw(self) -> None:
        cv, tr = self.canvas, self.tr
        cv.delete("all")
        width, height = cv.winfo_width(), self.HEIGHT
        if tr is None or width < 80:
            return
        c, font = self.th.c, self.th.font("chart")
        ml, mr, mt, mb = 40, 10, 10, 22
        now = self.now
        pts = [p for p in tr["points"] if p[0] >= now - self.WINDOW_S]
        limits = [v for v in (tr["max_limit"], tr["min_limit"]) if v is not None]
        values = [p[1] for p in pts] + limits
        lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
        if hi - lo < 1e-6:
            lo, hi = lo - 1.0, hi + 1.0
        pad = (hi - lo) * 0.1
        lo, hi = lo - pad, hi + pad
        ticks, step = nice_ticks(lo, hi)

        def x(t: float) -> float:
            return ml + (t - (now - self.WINDOW_S)) / self.WINDOW_S * (width - ml - mr)

        def y(v: float) -> float:
            return mt + (hi - v) / (hi - lo) * (height - mt - mb)

        for v in ticks:
            cv.create_line(ml, y(v), width - mr, y(v), fill=c["grid"])
            cv.create_text(ml - 6, y(v), text=f"{v:.0f}" if step >= 1 else f"{v:.1f}", anchor="e",
                           fill=c["muted"], font=font)
        cv.create_line(ml, height - mb, width - mr, height - mb, fill=c["axis"])
        for offset, label, anchor in self.X_TICKS:
            cv.create_text(x(now + offset), height - 2, text=label, anchor=anchor, fill=c["muted"], font=font)

        shade = blend(c["critical"], c["surface2"], 0.16 if self.exceeded else 0.08)
        u = unit(tr["unit"])
        for limit, above, name in ((tr["max_limit"], True, "상한"), (tr["min_limit"], False, "하한")):
            if limit is None:
                continue
            yl = y(limit)
            top, bottom = (mt, yl) if above else (yl, height - mb)
            if bottom > top:
                cv.create_rectangle(ml, top, width - mr, bottom, fill=shade, outline="")
            cv.create_line(ml, yl, width - mr, yl, fill=c["critical"], dash=(4, 3))
            label_below = not above and yl + 16 <= height - mb
            cv.create_text(ml + 4, yl + 9 if label_below else yl - 9, text=f"{name} {limit:g} {u}", anchor="w",
                           fill=c["text2"], font=font)

        if len(pts) >= 2:
            coords = [v for t, val in pts for v in (x(t), y(val))]
            cv.create_line(*coords, fill=c["series"], width=2, capstyle="round", joinstyle="round")
        if pts:
            tx, ty = x(pts[-1][0]), y(pts[-1][1])
            cv.create_oval(tx - 4, ty - 4, tx + 4, ty + 4, fill=c["series"], outline=c["surface2"], width=2)
        self._geom = (pts, x, y, mt, height - mb, width)
        self._draw_hover()

    def _on_motion(self, e: tk.Event) -> None:
        self.hover_x = e.x
        self._draw_hover()

    def _on_leave(self, _e: tk.Event) -> None:
        self.hover_x = None
        self._draw_hover()

    def _draw_hover(self) -> None:
        cv = self.canvas
        cv.delete("hover")
        if self.hover_x is None or not self._geom or not self._geom[0]:
            return
        pts, x, y, top, bottom, width = self._geom
        c, th = self.th.c, self.th
        t, v = min(pts, key=lambda p: abs(x(p[0]) - self.hover_x))
        cx, cy = x(t), y(v)
        cv.create_line(cx, top, cx, bottom, fill=c["text2"], tags="hover")
        cv.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=c["series"], outline=c["surface2"], width=2, tags="hover")
        lines = [(f"{v:.1f} {unit(self.tr['unit'])}", th.font("tip"), c["text"]),
                 (f"{self.tr['characteristic_id']} {self.tr['name']}", th.font("small"), c["text2"]),
                 (fmt_t(t), th.font("small"), c["text2"])]
        box_w = max(f.measure(text) for text, f, _ in lines) + 16
        box_h = sum(f.metrics("linespace") for _, f, _ in lines) + 10
        bx = cx + 12 if cx + 12 + box_w <= width - 2 else cx - 12 - box_w
        by = max(top, min(cy - box_h / 2, bottom - box_h))
        cv.create_rectangle(bx, by, bx + box_w, by + box_h, fill=c["surface"], outline=c["border"], tags="hover")
        ty = by + 5
        for text, f, color in lines:
            cv.create_text(bx + 8, ty, text=text, anchor="nw", font=f, fill=color, tags="hover")
            ty += f.metrics("linespace")
