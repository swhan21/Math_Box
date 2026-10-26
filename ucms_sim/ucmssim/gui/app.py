"""간이 HMI 데스크톱 화면 (tkinter, 설계안 6.8절 'HMI: Python GUI').

    ┌ 상단: 제목 · 모의 시각 · 화면 밝기 전환 ───────────────────────────────┐
    │ HMI (왼쪽, HMI가 DDS로 받은 값만)            │ 시험 하니스 (오른쪽, 시험용) │
    │  리소스 건전성 / 활성 고장(래치 해제) /      │  배속 · 속도 지령 · 고장 주입 │
    │  임계값 초과 / 특성값 추이 / 사용량·이벤트   │  재기동 · 플랜트 참값 · 기록  │
    └ 하단: 명령 결과 ───────────────────────────────────────────────────────┘

시뮬레이션은 이 창의 이벤트 루프가 50 ms마다 진행시키고(SimRuntime.pump), 화면은 300 ms마다 갱신한다.
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from ..config import Configs
from .runtime import CommandError, SimRuntime
from .theme import Theme
from .widgets import (
    EVENT_STATUS,
    SEVERITY,
    USAGE_LABELS,
    Chip,
    ScrollFrame,
    TrendChart,
    card,
    fmt_dur,
    fmt_t,
    fmt_v,
    unit,
)

SIM_SPEEDS = ((0.0, "정지"), (1.0, "1×"), (2.0, "2×"), (5.0, "5×"), (10.0, "10×"))
VEHICLE_SPEEDS = (0.0, 20.0, 40.0, 60.0)
TOGGLES = (("can_block", "FI-01", "인버터 CAN 차단"), ("sensor_open", "FI-04", "모터 온도센서 단선"),
           ("wheel_stuck", "FI-05", "좌측 휠속도 센서 고착"), ("estop", "U02", "비상정지"))
PROFILES = (("overtemp", "FI-02", "모터 과온 → 160 °C (래치)"), ("undervolt", "FI-03", "배터리 전압 강하 → 280 V"),
            ("inverter_hot", "C03", "인버터 과열 (임계값만)"), ("oscillate", "FI-07", "C01 임계값 근처 진동"),
            ("cabin_hot", "FI-10", "실내온도 상승 (브리지 장비)"))
ACTIONS = (("nvm_reboot", "FI-06", "NVM 손상 후 VCU 재기동"), ("vcu_reboot", "—", "VCU 재기동"),
           ("ucms_restart", "FI-08", "UCMS 재기동 (3 s 정지)"))
PLANT = (("speed_kph", "차속", "km/h"), ("motor_temp", "구동모터 온도", "°C"), ("inverter_temp", "인버터 온도", "°C"),
         ("battery_voltage", "배터리 전압", "V"), ("soc", "SOC", "%"), ("vcu_temp", "VCU 내부 온도", "°C"),
         ("cabin_temp", "실내 온도", "°C"))


class HmiApp:
    PUMP_MS = 50
    REFRESH_MS = 300

    def __init__(self, root: tk.Tk, runtime: SimRuntime, theme: str = "dark") -> None:
        self.root = root
        self.rt = runtime
        self.theme_name = theme
        self.main: tk.Frame | None = None
        self.show_table = False
        self._status: tuple[str, str] = ("muted", "준비")
        root.title("UCMS 간이 HMI — UGV 차량제어기 SIL")
        root.geometry("1560x980")
        root.minsize(1100, 700)
        self.build()
        self._last_refresh = 0.0
        self._after_id = root.after(self.PUMP_MS, self._tick)
        root.protocol("WM_DELETE_WINDOW", self.close)

    # -------------------------------------------------------------- 수명주기

    def _tick(self) -> None:
        self.rt.pump()
        now = time.monotonic()
        if now - self._last_refresh >= self.REFRESH_MS / 1000.0:
            self._last_refresh = now
            self.refresh()
        self._after_id = self.root.after(self.PUMP_MS, self._tick)

    def close(self) -> None:
        self.root.after_cancel(self._after_id)
        self.rt.stop()
        self.root.destroy()

    def toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.build()

    def send(self, action: str, **args: Any) -> None:
        try:
            self.set_status(self.rt.command(action, args), ok=True)
        except CommandError as e:
            self.set_status(str(e), ok=False)
        self.refresh()

    def set_status(self, text: str, ok: bool) -> None:
        self._status = ("good" if ok else "critical", f"{fmt_t(self.rt.tb.now)}  {text}")
        self.status.set(*self._status)

    # -------------------------------------------------------------- 화면 구성

    def build(self) -> None:
        if self.main is not None:
            self.main.destroy()
        self.th = th = Theme(self.root, self.theme_name)
        c = th.c
        self.root.configure(bg=c["page"])
        self.sig: dict[str, str] = {}
        self.charts: dict[str, TrendChart] = {}
        self.main = tk.Frame(self.root, bg=c["page"])
        self.main.pack(fill="both", expand=True)

        top = tk.Frame(self.main, bg=c["surface"], highlightbackground=c["border"], highlightthickness=1)
        top.pack(side="top", fill="x")
        tk.Label(top, text="UCMS 간이 HMI", bg=c["surface"], fg=c["text"], font=th.font("head")).pack(
            side="left", padx=(20, 8), pady=10)
        tk.Label(top, text=" UGV · SIL ", bg=c["surface2"], fg=c["text2"], font=th.font("mono")).pack(side="left")
        ttk.Button(top, text="밝은 화면" if self.theme_name == "dark" else "어두운 화면",
                   command=self.toggle_theme).pack(side="right", padx=(8, 20))
        self.clock = tk.Label(top, bg=c["surface"], fg=c["text"], font=th.font("clock"))
        self.clock.pack(side="right", padx=8)
        self.banner = tk.Label(self.main, bg=c["critical"], fg="#ffffff", font=th.font("title"), anchor="w", padx=20)

        bar = tk.Frame(self.main, bg=c["surface"], highlightbackground=c["border"], highlightthickness=1)
        bar.pack(side="bottom", fill="x")
        self.status = Chip(bar, th, c["surface"])
        self.status.pack(side="left", padx=16, pady=4)
        self.status.set(*self._status)

        body = tk.Frame(self.main, bg=c["page"])
        body.pack(side="top", fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        left = ScrollFrame(body, th)
        left.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=12)
        right = ScrollFrame(body, th, width=370)
        right.grid(row=0, column=1, sticky="ns", padx=(8, 16), pady=12)
        self._build_hmi(left.inner)
        self._build_harness(right.inner)
        self.refresh()

    def _tree(self, master: tk.Misc, columns: tuple[tuple[str, str, int, bool], ...], height: int,
              first: tuple[str, int] | None = None, headings: bool = True) -> ttk.Treeview:
        """columns = (id, 제목, 폭, 늘림). first = 아이콘 칸(#0) (제목, 폭)."""
        show = ("tree " if first else "") + ("headings" if headings else "")
        tree = ttk.Treeview(master, columns=[col[0] for col in columns], show=show.strip(), height=height,
                            selectmode="browse")
        if first:
            tree.heading("#0", text=first[0], anchor="w")
            tree.column("#0", width=first[1], minwidth=first[1], stretch=False, anchor="w")
        for cid, title, width, stretch in columns:
            tree.heading(cid, text=title, anchor="w")
            tree.column(cid, width=width, minwidth=40, stretch=stretch, anchor="w")
        return tree

    def _build_hmi(self, parent: tk.Frame) -> None:
        th, c = self.th, self.th.c
        pad = {"fill": "x", "pady": (0, 14)}

        frame, _, self.res_box = card(parent, th, "리소스 건전성", "Monitored_Resource")
        frame.pack(**pad)

        row = tk.Frame(parent, bg=c["page"])
        row.pack(**pad)
        row.columnconfigure(0, weight=1, uniform="r")
        row.columnconfigure(1, weight=1, uniform="r")
        frame, head, body = card(row, th, "활성 고장", "Fault_Event")
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self.fault_count = tk.Label(head, bg=c["surface"], fg=c["text2"], font=th.font("small"))
        self.fault_count.pack(side="right")
        self.fault_tree = self._tree(body, (("code", "코드", 50, False), ("desc", "내용", 220, True),
                                            ("bit", "BIT", 50, False), ("state", "상태", 60, False),
                                            ("raised", "발생", 96, False)), 4, ("심각도", 84))
        self.fault_tree.pack(fill="x")
        self.fault_tree.bind("<<TreeviewSelect>>", lambda e: self._update_clear_button())
        actions = tk.Frame(body, bg=c["surface"])
        actions.pack(fill="x", pady=(8, 0))
        self.clear_btn = ttk.Button(actions, text="선택한 래치 고장 해제", style="Primary.TButton",
                                    command=self._clear_selected)
        self.clear_btn.pack(side="left")
        tk.Label(actions, text="래치 고장만 운용자 해제 (Fault_Code__clearFault)", bg=c["surface"], fg=c["text2"],
                 font=th.font("small")).pack(side="left", padx=10)

        frame, head, body = card(row, th, "임계값 초과", "Threshold_Exceedance_Event")
        frame.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self.th_count = tk.Label(head, bg=c["surface"], fg=c["text2"], font=th.font("small"))
        self.th_count.pack(side="right")
        self.th_tree = self._tree(body, (("char", "리소스/특성", 240, True), ("limit", "한계", 40, False),
                                         ("value", "초과 값", 56, False), ("time", "발생", 90, False)),
                                  4, ("구분", 96))
        self.th_tree.pack(fill="x")

        frame, head, body = card(parent, th, "특성값 추이", "최근 3분 · 점선 = 임계값 · 커서를 올리면 값 표시")
        frame.pack(**pad)
        self.table_btn = ttk.Button(head, text="차트로 보기" if self.show_table else "표로 보기",
                                    command=self._toggle_table)
        self.table_btn.pack(side="right")
        self.chart_box = tk.Frame(body, bg=c["surface"])
        for col in range(3):
            self.chart_box.columnconfigure(col, weight=1, uniform="ch")
        self.trend_tree = self._tree(body, (("cur", "현재", 90, False), ("min", "3분 최소", 80, False),
                                            ("max", "3분 최대", 80, False), ("limit", "임계값", 110, False),
                                            ("state", "상태", 90, False), ("provider", "사양", 70, True)),
                                     6, ("특성", 260))
        (self.trend_tree if self.show_table else self.chart_box).pack(fill="x")

        row = tk.Frame(parent, bg=c["page"])
        row.pack(**pad)
        row.columnconfigure(0, weight=1, uniform="r")
        row.columnconfigure(1, weight=1, uniform="r")
        frame, _, self.usage_box = card(row, th, "사용량", "Characteristic_Usage")
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        frame, _, body = card(row, th, "이벤트 기록", "HMI 수신 순 (최신이 위)")
        frame.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self.event_tree = self._tree(body, (("text", "내용", 360, True),), 9, ("시각", 120), headings=False)
        self.event_tree.pack(fill="x")

    def _build_harness(self, parent: tk.Frame) -> None:
        th, c = self.th, self.th.c
        frame = tk.Frame(parent, bg=c["surface"], highlightbackground=c["border"], highlightthickness=1)
        frame.pack(fill="x")
        head = tk.Frame(frame, bg=c["surface2"])
        head.pack(fill="x")
        tk.Label(head, text="시험 하니스", bg=c["surface2"], fg=c["text"], font=th.font("title"), anchor="w").pack(
            fill="x", padx=16, pady=(12, 0))
        tk.Label(head, text="고장 주입·시험 제어 — HMI 기능 아님 (시험용)", bg=c["surface2"], fg=c["text2"],
                 font=th.font("small"), anchor="w").pack(fill="x", padx=16, pady=(0, 10))

        def section(title: str) -> tk.Frame:
            sec = tk.Frame(frame, bg=c["surface"])
            sec.pack(fill="x", padx=16, pady=(10, 4))
            tk.Label(sec, text=title, bg=c["surface"], fg=c["text2"], font=th.font("small"), anchor="w").pack(
                fill="x", pady=(0, 4))
            return sec

        def control_row(sec: tk.Frame, ident: str, label: str) -> tk.Frame:
            row = tk.Frame(sec, bg=c["surface"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=ident, width=6, anchor="w", bg=c["surface"], fg=c["text2"],
                     font=th.font("mono")).pack(side="left")
            tk.Label(row, text=label, anchor="w", bg=c["surface"], fg=c["text"], font=th.font("base")).pack(
                side="left", fill="x", expand=True)
            return row

        sec = section("상태")
        self.vcu_chip = Chip(sec, th, c["surface"])
        self.vcu_chip.pack(anchor="w")
        self.ucms_chip = Chip(sec, th, c["surface"])
        self.ucms_chip.pack(anchor="w")

        sec = section("배속")
        self.v_sim = tk.DoubleVar(value=self.rt.speed)
        seg = tk.Frame(sec, bg=c["surface"])
        seg.pack(anchor="w")
        for value, label in SIM_SPEEDS:
            ttk.Radiobutton(seg, text=label, value=value, variable=self.v_sim, style="Seg.Toolbutton",
                            command=lambda: self.send("sim_speed", factor=self.v_sim.get())).pack(
                side="left", padx=(0, 4))

        sec = section("차량 속도 지령 (km/h)")
        self.v_veh = tk.DoubleVar(value=self.rt.tb.vcu.speed_setpoint_kph)
        seg = tk.Frame(sec, bg=c["surface"])
        seg.pack(anchor="w")
        for value in VEHICLE_SPEEDS:
            ttk.Radiobutton(seg, text=f"{value:g}", value=value, variable=self.v_veh, style="Seg.Toolbutton",
                            command=lambda: self.send("speed", kph=self.v_veh.get())).pack(side="left", padx=(0, 4))

        sec = section("신호·통신 고장 (켜고 끄기)")
        self.v_toggles: dict[str, tk.BooleanVar] = {}
        self.toggle_btns: dict[str, ttk.Checkbutton] = {}
        for name, ident, label in TOGGLES:
            var = self.v_toggles[name] = tk.BooleanVar(value=False)
            check = ttk.Checkbutton(control_row(sec, ident, label), variable=var, text="켬",
                                    command=lambda n=name, v=var: self.send(n, on=v.get()))
            check.pack(side="right")
            self.toggle_btns[name] = check

        sec = section("플랜트 프로파일")
        self.profile_btns: dict[str, ttk.Button] = {}
        for name, ident, label in PROFILES:
            btn = ttk.Button(control_row(sec, ident, label), text="주입", width=7, command=lambda n=name: self.send(n))
            btn.pack(side="right")
            self.profile_btns[name] = btn

        sec = section("기동·서비스")
        for name, ident, label in ACTIONS:
            ttk.Button(control_row(sec, ident, label), text="실행", width=7,
                       command=lambda n=name: self.send(n)).pack(side="right")

        sec = section("플랜트 참값 (HMI에는 보이지 않음)")
        grid = tk.Frame(sec, bg=c["surface"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        self.plant_labels: dict[str, tk.Label] = {}
        for i, (key, label, _) in enumerate(PLANT):
            tk.Label(grid, text=label, bg=c["surface"], fg=c["text2"], font=th.font("base"), anchor="w").grid(
                row=i, column=0, sticky="w")
            value = tk.Label(grid, bg=c["surface"], fg=c["text"], font=th.font("base"), anchor="e")
            value.grid(row=i, column=1, sticky="e")
            self.plant_labels[key] = value

        sec = section("하니스 기록")
        self.hlog = tk.Listbox(sec, height=9, bg=c["surface"], fg=c["text2"], font=th.font("small"), borderwidth=0,
                               highlightthickness=0, activestyle="none", selectbackground=c["select"],
                               selectforeground=c["text"])
        self.hlog.pack(fill="x", pady=(0, 12))

    # -------------------------------------------------------------- 갱신

    def _changed(self, key: str, value: Any) -> bool:
        sig = repr(value)
        if self.sig.get(key) == sig:
            return False
        self.sig[key] = sig
        return True

    def refresh(self) -> None:
        snap = self.rt.snapshot()
        hmi, hs = snap["hmi"], snap["harness"]
        self.clock.configure(text=fmt_t(hmi["t"]))
        if hs["error"]:
            self.banner.configure(text=f"시뮬레이션 오류로 정지: {hs['error']}")
            self.banner.pack(side="top", fill="x", after=self.main.winfo_children()[0])
        self._render_resources(hmi["resources"])
        self._render_faults(hmi["faults"])
        self._render_thresholds(hmi["thresholds"])
        self._render_trends(hmi)
        self._render_usage(hmi["usage"])
        self._render_events(hmi["events"])
        self._render_harness(hs)

    def _render_resources(self, resources: list[dict]) -> None:
        if not self._changed("res", resources):
            return
        th, c = self.th, self.th.c
        for child in self.res_box.winfo_children():
            child.destroy()
        for i, r in enumerate(resources):
            if r["health"] == "RED":
                status, label, glyph = "critical", "고장 (RED)", "!"
            elif r["health"] == "GREEN":
                status, label, glyph = "good", "정상 (GREEN)", "✓"
            else:
                status, label, glyph = "muted", "판정 없음", "–"
            if r["health"] is None:
                meta = f"R-role 미지원 · 사양 출처 {'브리지' if r['provider'] == 'BRIDGE' else r['provider'] or '-'}"
            else:
                meta = "PBIT 진행 중" if not r["pbit_complete"] else ("PBIT 통과" if r["pbit_passed"] else "PBIT 실패")
            if r["active_faults"]:
                meta += f" · 활성 고장 {r['active_faults']}건"
            color = c[status]
            box = tk.Frame(self.res_box, bg=c["surface2"], highlightbackground=c["border"], highlightthickness=1)
            box.grid(row=0, column=i, sticky="w", padx=(0, 12))
            tk.Frame(box, bg=color, width=4).pack(side="left", fill="y")
            icon = tk.Canvas(box, width=40, height=40, bg=c["surface2"], highlightthickness=0)
            icon.pack(side="left", padx=12, pady=10)
            icon.create_oval(1, 1, 39, 39, fill=color, outline="")
            icon.create_text(20, 20, text=glyph, fill="#ffffff", font=th.font("icon"))
            text = tk.Frame(box, bg=c["surface2"])
            text.pack(side="left", padx=(0, 16), pady=8)
            tk.Label(text, text=r["resource_id"], bg=c["surface2"], fg=c["text2"], font=th.font("mono"),
                     anchor="w").pack(fill="x")
            tk.Label(text, text=label, bg=c["surface2"], fg=c["text"], font=th.font("state"), anchor="w").pack(fill="x")
            tk.Label(text, text=meta, bg=c["surface2"], fg=c["text2"], font=th.font("small"), anchor="w").pack(fill="x")

    def _render_faults(self, faults: list[dict]) -> None:
        self.fault_count.configure(text=f"{len(faults)}건" if faults else "없음")
        if not self._changed("faults", faults):
            return
        tree = self.fault_tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for f in faults:
            status, label = SEVERITY.get(f["severity"], ("muted", "미상"))
            tree.insert("", "end", iid=f["event_id"], text=f" {label}", image=self.th.dot(status),
                        values=(f["fault_code_id"], f"{f['description']}  ({f['event_id']})", f["bit_type"],
                                "래치" if f["latched"] else "활성", fmt_t(f["raised_time"])))
        keep = [iid for iid in selected if tree.exists(iid)]
        latched = [f["event_id"] for f in faults if f["latched"]]
        if keep:
            tree.selection_set(keep)
        elif len(latched) == 1:
            tree.selection_set(latched)                      # 래치 고장이 하나면 바로 해제할 수 있게 선택
        self._faults = {f["event_id"]: f for f in faults}
        self._update_clear_button()

    def _update_clear_button(self) -> None:
        selected = self.fault_tree.selection()
        fault = getattr(self, "_faults", {}).get(selected[0]) if selected else None
        self.clear_btn.state(["!disabled"] if fault and fault["latched"] else ["disabled"])

    def _clear_selected(self) -> None:
        selected = self.fault_tree.selection()
        fault = self._faults.get(selected[0]) if selected else None
        if fault and fault["latched"]:
            self.send("clear_fault", resource_id=fault["resource_id"], fault_code_id=fault["fault_code_id"])

    def _render_thresholds(self, events: list[dict]) -> None:
        self.th_count.configure(text=f"{len(events)}건" if events else "없음")
        if not self._changed("th", events):
            return
        tree = self.th_tree
        tree.delete(*tree.get_children())
        for e in events:
            tree.insert("", "end", iid=e["event_id"], image=self.th.dot("critical"),
                        text=" 상한 초과" if e["limit_type"] == "MAX" else " 하한 미달",
                        values=(f"{e['resource_id']}/{e['characteristic_id']}", f"{e['limit_value']:g}",
                                fmt_v(e["trigger_value"]), fmt_t(e["exceeded_time"])))

    def _toggle_table(self) -> None:
        self.show_table = not self.show_table
        self.table_btn.configure(text="차트로 보기" if self.show_table else "표로 보기")
        (self.chart_box if self.show_table else self.trend_tree).pack_forget()
        (self.trend_tree if self.show_table else self.chart_box).pack(fill="x")
        self.sig.pop("trend_table", None)
        self.refresh()

    def _render_trends(self, hmi: dict) -> None:
        exceeded = {f"{e['resource_id']}/{e['characteristic_id']}" for e in hmi["thresholds"]}
        rows = []
        for tr in hmi["trends"]:
            key = f"{tr['resource_id']}/{tr['characteristic_id']}"
            last = tr["points"][-1][1] if tr["points"] else None
            if not self.show_table:
                chart = self.charts.get(key)
                if chart is None:
                    chart = self.charts[key] = TrendChart(self.chart_box, self.th)
                    n = len(self.charts) - 1
                    chart.frame.grid(row=n // 3, column=n % 3, sticky="nsew", padx=6, pady=6)
                chart.set(tr, hmi["t"], key in exceeded)
            window = [v for t, v in tr["points"] if t >= hmi["t"] - TrendChart.WINDOW_S]
            limits = ", ".join(s for s in (f"≤ {tr['max_limit']:g}" if tr["max_limit"] is not None else "",
                                           f"≥ {tr['min_limit']:g}" if tr["min_limit"] is not None else "") if s)
            if key in exceeded:
                status, state = "critical", "임계 초과"
            elif tr["stale"] or last is None:
                status, state = "muted", "갱신 지연" if last is not None else "값 없음"
            else:
                status, state = "good", "정상 범위"
            u = unit(tr["unit"])
            name = tr["name"] if tr["name"] == tr["characteristic_id"] else f"{tr['characteristic_id']} {tr['name']}"
            rows.append((key, status, f" {name}",
                         (f"{fmt_v(last)} {u}", fmt_v(min(window)) if window else "—",
                          fmt_v(max(window)) if window else "—", f"{limits} {u}" if limits else "—", state,
                          "브리지" if tr["provider"] == "BRIDGE" else "리소스")))
        if self.show_table and self._changed("trend_table", rows):
            tree = self.trend_tree
            tree.delete(*tree.get_children())
            for key, status, text, values in rows:
                tree.insert("", "end", iid=key, text=text, image=self.th.dot(status), values=values)

    def _render_usage(self, usage: list[dict]) -> None:
        if not self._changed("usage", usage):
            return
        th, c = self.th, self.th.c
        for child in self.usage_box.winfo_children():
            child.destroy()
        if not usage:
            tk.Label(self.usage_box, text="수신된 사용량 없음", bg=c["surface"], fg=c["muted"],
                     font=th.font("base")).pack(anchor="w")
            return
        for i, u in enumerate(usage):
            tile = tk.Frame(self.usage_box, bg=c["surface2"], highlightbackground=c["border"], highlightthickness=1)
            tile.grid(row=0, column=i, sticky="nw", padx=(0, 12))
            value = fmt_dur(u["value"]) if u["unit"] == "s" else f"{u['value']:.0f}{unit(u['unit'])}"
            for text, font, fg in ((f"{USAGE_LABELS.get(u['usage_type'], u['usage_type'])} ({u['usage_id']})",
                                    "small", "text2"), (value, "tile", "text"), (u["resource_id"], "mono", "text2")):
                tk.Label(tile, text=text, bg=c["surface2"], fg=c[fg], font=th.font(font), anchor="w").pack(
                    fill="x", padx=12, pady=(6 if font == "small" else 0, 6 if font == "mono" else 0))

    def _render_events(self, events: list[dict]) -> None:
        if not self._changed("events", events):
            return
        tree = self.event_tree
        tree.delete(*tree.get_children())
        for i, e in enumerate(events):
            status = EVENT_STATUS.get(e["kind"], "muted")
            tree.insert("", "end", iid=str(i), text=f" {fmt_t(e['t'])}", image=self.th.dot(status), values=(e["text"],))

    def _render_harness(self, hs: dict) -> None:
        if self.v_sim.get() != hs["sim_speed"]:
            self.v_sim.set(hs["sim_speed"])
        if self.v_veh.get() != hs["setpoint_kph"]:
            self.v_veh.set(hs["setpoint_kph"])
        for name, var in self.v_toggles.items():
            if var.get() != hs["toggles"][name]:
                var.set(hs["toggles"][name])
        for name, btn in self.profile_btns.items():
            busy = hs["busy"][name]
            btn.configure(text="진행 중" if busy else "주입")
            btn.state(["disabled"] if busy else ["!disabled"])
        vcu_status = "good" if hs["vcu"] == "운용" else "critical" if hs["vcu"] == "전원 차단" else "warning"
        self.vcu_chip.set(vcu_status, f"VCU {hs['vcu']} (부팅 {hs['boot_count']}회)")
        self.ucms_chip.set("good" if hs["ucms"] == "동작" else "critical", f"UCMS {hs['ucms']}")
        for key, _, u in PLANT:
            self.plant_labels[key].configure(text=f"{hs['plant'][key]:.1f} {u}")
        if self._changed("hlog", hs["log"]):
            self.hlog.delete(0, "end")
            for e in hs["log"]:
                self.hlog.insert("end", f"{fmt_t(e['t'])}  {e['text']}")


def run_app(cfg: Configs, speed: float, db_path: Path, theme: str = "dark") -> None:
    runtime = SimRuntime(cfg, db_path, speed=speed)
    runtime.start()
    root = tk.Tk()
    HmiApp(root, runtime, theme)
    print(f"UCMS 간이 HMI 실행 (배속 {speed:g}×, DB {db_path}) — 창을 닫으면 종료")
    root.mainloop()
