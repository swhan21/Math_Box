"""제어 SW → UCM 어댑터 단방향 진단 채널 (설계안 6.3절 '제어 SW ↔ 어댑터 인터페이스').

  - 진단 기록(DTC 전이·PBIT 결과·이벤트): 고정 크기 링버퍼. 가득 차면 가장 오래된 기록을 버린다.
  - 특성값: 최신값 공유 메모리(스냅샷). 매 제어 주기 덮어쓴다.
쓰기 쪽(제어 SW)은 절대 블록되지 않으며 어댑터 상태와 무관하게 동작한다 (단방향 결합).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DtcRecord:
    t: float
    dtc: int
    active: bool


@dataclass(frozen=True)
class PbitRecord:
    t: float
    passed: bool


@dataclass(frozen=True)
class EventRecord:
    t: float
    name: str


class DiagnosticChannel:
    def __init__(self, size: int = 256) -> None:
        self._ring: deque = deque(maxlen=size)
        self.overflow = 0
        self._signals: dict[str, Any] = {}
        self._signals_t: float | None = None

    # -------------------------------------------------------------- 제어 SW 측 (쓰기)

    def post_dtc(self, dtc: int, active: bool, t: float) -> None:
        self._put(DtcRecord(t, dtc, active))

    def post_pbit(self, passed: bool, t: float) -> None:
        self._put(PbitRecord(t, passed))

    def post_event(self, name: str, t: float) -> None:
        self._put(EventRecord(t, name))

    def publish_signals(self, t: float, signals: dict[str, Any]) -> None:
        self._signals = dict(signals)
        self._signals_t = t

    def _put(self, record: Any) -> None:
        if len(self._ring) == self._ring.maxlen:
            self.overflow += 1
        self._ring.append(record)

    # -------------------------------------------------------------- 어댑터 측 (읽기)

    def drain(self) -> list[Any]:
        records = list(self._ring)
        self._ring.clear()
        return records

    @property
    def pending(self) -> int:
        return len(self._ring)

    def snapshot(self) -> tuple[float | None, dict[str, Any]]:
        return self._signals_t, self._signals
