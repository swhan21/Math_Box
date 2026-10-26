"""HMI 화면의 색·글꼴·ttk 스타일.

색 규칙 (데이터 시각화 기준 팔레트)
  - 계열색 1개: 특성값 선. 어두운/밝은 화면 각각 표면 대비를 검증한 단계를 쓴다.
  - 상태색 4개 (good·warning·serious·critical): 상태 의미에만 쓰고, 항상 색 점 + 글자로 표시한다.
  - 글자는 본문색(text·text2·muted)만 쓴다. 상태색으로 글자를 칠하지 않는다.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "page": "#0d0d0d", "surface": "#1a1a19", "surface2": "#222220", "text": "#f0efec", "text2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "border": "#323230", "series": "#3987e5",
        "series_hover": "#5598e7", "btn": "#2a2a28", "btn_hover": "#353532", "select": "#23384f",
    },
    "light": {
        "page": "#f9f9f7", "surface": "#fcfcfb", "surface2": "#f3f2ef", "text": "#0b0b0b", "text2": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "border": "#e3e3e1", "series": "#2a78d6",
        "series_hover": "#256abf", "btn": "#ecebe7", "btn_hover": "#e1e0d9", "select": "#d6e5f7",
    },
}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

UI_FONTS = ("Malgun Gothic", "맑은 고딕", "Apple SD Gothic Neo", "Noto Sans CJK KR", "Noto Sans KR",
            "NanumGothic", "WenQuanYi Zen Hei", "Segoe UI", "Helvetica")
MONO_FONTS = ("D2Coding", "Consolas", "Menlo", "JetBrains Mono", "DejaVu Sans Mono", "Courier New")

# 글꼴 크기는 픽셀(음수) 단위 — 화면 DPI 설정과 무관하게 같은 배치
FONT_SPECS = {
    "base": ("ui", -14, "normal"), "small": ("ui", -12, "normal"), "title": ("ui", -14, "bold"),
    "head": ("ui", -16, "bold"), "state": ("ui", -20, "bold"), "value": ("ui", -20, "bold"),
    "tile": ("ui", -22, "bold"), "chart": ("ui", -11, "normal"), "tip": ("ui", -14, "bold"),
    "icon": ("ui", -20, "bold"), "mono": ("mono", -12, "normal"), "clock": ("mono", -15, "normal"),
}


def blend(fg: str, bg: str, alpha: float) -> str:
    """fg 를 alpha 만큼 bg 위에 칠한 색 (Tk 캔버스에는 투명도가 없음)."""
    f = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(fc * alpha + bc * (1 - alpha)):02x}" for fc, bc in zip(f, b, strict=True))


def _pick(families: set[str], candidates: tuple[str, ...], fallback: str) -> str:
    return next((name for name in candidates if name in families), fallback)


def _dot_image(master: tk.Misc, color: str, size: int = 10) -> tk.PhotoImage:
    img = tk.PhotoImage(master=master, width=size, height=size)
    r = size / 2
    for row in range(size):
        dy = row + 0.5 - r
        if dy * dy >= r * r:
            continue
        half = (r * r - dy * dy) ** 0.5
        x0, x1 = round(r - half), round(r + half)
        if x1 > x0:
            img.put(color, to=(x0, row, x1, row + 1))
    return img


class Theme:
    def __init__(self, root: tk.Tk, name: str) -> None:
        self.name = name
        self.c = {**PALETTES[name], **STATUS}
        families = set(tkfont.families(root))
        default = tkfont.nametofont("TkDefaultFont").actual("family")
        family = {"ui": _pick(families, UI_FONTS, default), "mono": _pick(families, MONO_FONTS, "TkFixedFont")}
        self.fonts = {key: tkfont.Font(root, family=family[kind], size=size, weight=weight)
                      for key, (kind, size, weight) in FONT_SPECS.items()}
        dot_colors = {**STATUS, "muted": self.c["muted"], "neutral": self.c["series"]}
        self._dots = {key: _dot_image(root, color) for key, color in dot_colors.items()}
        self._style(root)

    def font(self, name: str) -> tkfont.Font:
        return self.fonts[name]

    def dot(self, status: str) -> tk.PhotoImage:
        return self._dots.get(status, self._dots["muted"])

    def _style(self, root: tk.Tk) -> None:
        c, f = self.c, self.fonts
        s = ttk.Style(root)
        s.theme_use("clam")
        s.configure(".", background=c["surface"], foreground=c["text"], font=f["base"], bordercolor=c["border"],
                    focuscolor=c["series"], troughcolor=c["page"])
        s.configure("TButton", background=c["btn"], foreground=c["text"], lightcolor=c["btn"], darkcolor=c["btn"],
                    padding=(10, 3))
        s.map("TButton", background=[("disabled", c["surface2"]), ("active", c["btn_hover"])],
              foreground=[("disabled", c["muted"])])
        s.configure("Primary.TButton", background=c["series"], foreground="#ffffff", lightcolor=c["series"],
                    darkcolor=c["series"], bordercolor=c["series"], font=f["title"])
        s.map("Primary.TButton", background=[("disabled", c["btn"]), ("active", c["series_hover"])],
              foreground=[("disabled", c["muted"])], bordercolor=[("disabled", c["border"])],
              lightcolor=[("disabled", c["btn"])], darkcolor=[("disabled", c["btn"])])
        s.configure("Seg.Toolbutton", background=c["btn"], foreground=c["text"], padding=(10, 3), anchor="center")
        s.map("Seg.Toolbutton", background=[("selected", c["series"]), ("active", c["btn_hover"])],
              foreground=[("selected", "#ffffff")])
        s.configure("TCheckbutton", background=c["surface"], indicatorbackground=c["surface2"],
                    indicatorforeground="#ffffff", upperbordercolor=c["muted"], lowerbordercolor=c["muted"])
        s.map("TCheckbutton", indicatorbackground=[("selected", c["critical"])],
              background=[("active", c["surface"])])
        s.configure("Treeview", background=c["surface"], fieldbackground=c["surface"], foreground=c["text"],
                    rowheight=30, borderwidth=1, bordercolor=c["grid"], lightcolor=c["surface"],
                    darkcolor=c["surface"], font=f["base"])
        s.map("Treeview", background=[("selected", c["select"])], foreground=[("selected", c["text"])])
        s.configure("Treeview.Heading", background=c["surface"], foreground=c["text2"], font=f["small"],
                    relief="flat", bordercolor=c["grid"], lightcolor=c["surface"], darkcolor=c["surface"])
        s.map("Treeview.Heading", background=[("active", c["surface2"])])
        s.configure("Vertical.TScrollbar", background=c["btn"], troughcolor=c["page"], bordercolor=c["page"],
                    arrowcolor=c["text2"], lightcolor=c["btn"], darkcolor=c["btn"])
        s.map("Vertical.TScrollbar", background=[("active", c["btn_hover"])])
