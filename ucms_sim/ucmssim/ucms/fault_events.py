"""Fault Event Deriver: Fault_Code + 사양 → Fault_Event 생성·해제 (설계안 6.4절).

Fault_Event 는 UCMS만 발행한다. 상태 전이마다 '저장 후 발행'하며,
재기동 시 DB의 활성 이벤트를 복원·재발행해 늦은 접속자도 현재 상태를 받게 한다 (SC6).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from ..topics import EventState, FaultCode, FaultCodeSpecification, FaultEvent, FaultState
from .persistence import UcmsStore
from .spec_registry import SpecRegistry

Key = tuple[str, str]


class FaultEventDeriver:
    def __init__(
        self,
        store: UcmsStore,
        registry: SpecRegistry,
        publish: Callable[[Any], None],
        dispose: Callable[[Any], None],
        now: Callable[[], float],
        call_later: Callable[..., Any],
        spec_wait_s: float,
        log: Callable[[str, str], None],
    ) -> None:
        self.store = store
        self.registry = registry
        self.publish = publish
        self.dispose = dispose
        self.now = now
        self.call_later = call_later
        self.spec_wait_s = spec_wait_s
        self.log = log
        self.active: dict[Key, FaultEvent] = {}
        self._pending: dict[Key, list[FaultCode]] = {}

    def restore(self) -> None:
        for ev in self.store.active_fault_events():
            self.active[(ev.resource_id, ev.fault_code_id)] = ev
            self.publish(ev)
        if self.active:
            self.log("INFO", f"활성 고장 이벤트 {len(self.active)}건 복원")

    # -------------------------------------------------------------- 수신

    def on_fault_code(self, fc: FaultCode) -> None:
        key = (fc.resource_id, fc.fault_code_id)
        if key in self._pending:
            self._pending[key].append(fc)
            return
        spec = self.registry.fault_spec(*key)
        if spec is None and fc.state is not FaultState.INACTIVE:
            self._pending[key] = [fc]                   # 사양 도착 대기 (편차 DEV-011)
            self.call_later(self.spec_wait_s, self._pending_timeout, key)
            return
        self._apply(fc, spec)

    def on_fault_spec(self, spec: FaultCodeSpecification) -> None:
        key = (spec.resource_id, spec.fault_code_id)
        for fc in self._pending.pop(key, []):
            self._apply(fc, spec)

    def _pending_timeout(self, key: Key) -> None:
        codes = self._pending.pop(key, None)
        if codes:
            self.log("WARN", f"{key[0]}/{key[1]} 고장코드 사양 미수신 — UNKNOWN 사양으로 처리")
            for fc in codes:
                self._apply(fc, None)

    # -------------------------------------------------------------- 도출

    def _apply(self, fc: FaultCode, spec: FaultCodeSpecification | None) -> None:
        key = (fc.resource_id, fc.fault_code_id)
        cur = self.active.get(key)
        now = self.now()
        if fc.state is FaultState.INACTIVE:
            if cur is None:
                return
            del self.active[key]
            ev = replace(cur, state=EventState.CLEARED, latched=False, cleared_time=now, source_timestamp=fc.timestamp)
            self._emit(ev)
            self.dispose(ev)
            return

        latched = fc.state is FaultState.LATCHED
        if cur is None:
            ev = FaultEvent(
                event_id=self.store.next_event_id("FE"),
                resource_id=fc.resource_id,
                fault_code_id=fc.fault_code_id,
                description=spec.description if spec else "UNKNOWN (사양 미수신)",
                severity=spec.severity.value if spec else "unknown",
                bit_type=fc.bit_type,
                state=EventState.ACTIVE,
                latched=latched,
                raised_time=now,
                cleared_time=None,
                source_timestamp=fc.timestamp,
            )
        elif cur.latched != latched:
            ev = replace(cur, latched=latched, source_timestamp=fc.timestamp)
        else:
            return                                      # 같은 상태 재수신 (재기동 재생 등)
        self.active[key] = ev
        self._emit(ev)

    def _emit(self, ev: FaultEvent) -> None:
        self.store.record_fault_event(self.now(), ev)  # 저장 후 발행
        self.publish(ev)
