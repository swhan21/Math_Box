"""UCMS 저장소 (SQLite): 이벤트, 고장, 특성값 샘플, 사용량, 편차 로그.

이벤트는 '저장 후 발행' 순서로 처리한다 (발행된 이벤트는 반드시 DB에 있음 → SC5).
event_log 는 발행한 이벤트 상태 전이마다 1행씩 쌓이는 원장이다.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from ..deviation import Deviation
from ..topics import (
    CharacteristicUsage,
    CharacteristicValue,
    FaultEvent,
    MonitoredResource,
    ResourceRegistration,
    ThresholdExceedanceEvent,
    decode,
    encode,
    fault_event_tag,
    threshold_event_tag,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS resource (
    resource_id TEXT PRIMARY KEY, name TEXT, kind TEXT, boot_count INTEGER, registered_time REAL
);
CREATE TABLE IF NOT EXISTS spec_adoption (
    id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, resource_id TEXT, item_id TEXT,
    provider TEXT, rationale TEXT
);
CREATE TABLE IF NOT EXISTS fault_event (
    event_id TEXT PRIMARY KEY, resource_id TEXT, fault_code_id TEXT, description TEXT,
    severity TEXT, bit_type TEXT, state TEXT, latched INTEGER, raised_time REAL,
    cleared_time REAL, payload TEXT
);
CREATE TABLE IF NOT EXISTS threshold_event (
    event_id TEXT PRIMARY KEY, resource_id TEXT, characteristic_id TEXT, limit_type TEXT,
    limit_value REAL, trigger_value REAL, release_value REAL, state TEXT, spec_provider TEXT,
    exceeded_time REAL, released_time REAL, payload TEXT
);
CREATE TABLE IF NOT EXISTS event_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, topic TEXT, event_id TEXT, tag TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS health_log (
    t REAL, resource_id TEXT, health TEXT, pbit_complete INTEGER, pbit_passed INTEGER
);
CREATE TABLE IF NOT EXISTS characteristic_sample (
    t REAL, resource_id TEXT, characteristic_id TEXT, value REAL
);
CREATE TABLE IF NOT EXISTS usage_history (
    t REAL, resource_id TEXT, usage_id TEXT, usage_type TEXT, value REAL, unit TEXT
);
CREATE TABLE IF NOT EXISTS service_log (
    t REAL, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS deviation_log (
    dev_id TEXT PRIMARY KEY, item TEXT, reference TEXT, implementation TEXT, reason TEXT
);
"""


class UcmsStore:
    def __init__(self, path: str) -> None:
        self.path = path
        # 웹 GUI에서는 스레드가 바뀌어도 호출이 잠금으로 직렬화되므로 스레드 검사를 끈다
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self._samples: list[tuple] = []

    def close(self) -> None:
        self.flush()
        self.conn.close()

    def flush(self) -> None:
        if self._samples:
            self.conn.executemany("INSERT INTO characteristic_sample VALUES (?,?,?,?)", self._samples)
            self._samples.clear()
        self.conn.commit()

    # -------------------------------------------------------------- 기록

    def write_deviations(self, devs: Iterable[Deviation]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO deviation_log VALUES (?,?,?,?,?)",
            [(d.id, d.item, d.reference, d.implementation, d.reason) for d in devs],
        )
        self.conn.commit()

    def log(self, t: float, level: str, message: str) -> None:
        self.conn.execute("INSERT INTO service_log VALUES (?,?,?)", (t, level, message))
        self.conn.commit()

    def upsert_resource(self, t: float, reg: ResourceRegistration) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO resource VALUES (?,?,?,?,?)",
            (reg.resource_id, reg.name, reg.kind, reg.boot_count, t),
        )
        self.conn.commit()

    def log_spec_adoption(self, t: float, resource_id: str, item_id: str, provider: str, rationale: str) -> None:
        self.conn.execute(
            "INSERT INTO spec_adoption (t, resource_id, item_id, provider, rationale) VALUES (?,?,?,?,?)",
            (t, resource_id, item_id, provider, rationale),
        )
        self.conn.commit()

    def next_event_id(self, prefix: str) -> str:
        table = {"FE": "fault_event", "TE": "threshold_event"}[prefix]
        (n,) = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return f"{prefix}-{n + 1:05d}"

    def record_fault_event(self, t: float, ev: FaultEvent) -> None:
        payload = encode(ev).decode("utf-8")
        self.conn.execute(
            "INSERT OR REPLACE INTO fault_event VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ev.event_id, ev.resource_id, ev.fault_code_id, ev.description, ev.severity,
             ev.bit_type.value, ev.state.value, int(ev.latched), ev.raised_time, ev.cleared_time, payload),
        )
        self._append_log(t, FaultEvent.TOPIC, ev.event_id, fault_event_tag(ev), payload)

    def record_threshold_event(self, t: float, ev: ThresholdExceedanceEvent) -> None:
        payload = encode(ev).decode("utf-8")
        self.conn.execute(
            "INSERT OR REPLACE INTO threshold_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.event_id, ev.resource_id, ev.characteristic_id, ev.limit_type.value, ev.limit_value,
             ev.trigger_value, ev.release_value, ev.state.value, ev.spec_provider.value,
             ev.exceeded_time, ev.released_time, payload),
        )
        self._append_log(t, ThresholdExceedanceEvent.TOPIC, ev.event_id, threshold_event_tag(ev), payload)

    def _append_log(self, t: float, topic: str, event_id: str, tag: str, payload: str) -> None:
        self.conn.execute(
            "INSERT INTO event_log (t, topic, event_id, tag, payload) VALUES (?,?,?,?,?)",
            (t, topic, event_id, tag, payload),
        )
        self.conn.commit()

    def log_health(self, t: float, mr: MonitoredResource) -> None:
        self.conn.execute(
            "INSERT INTO health_log VALUES (?,?,?,?,?)",
            (t, mr.resource_id, mr.health.value, int(mr.pbit_complete), int(mr.pbit_passed)),
        )
        self.conn.commit()

    def add_sample(self, t: float, cv: CharacteristicValue) -> None:
        self._samples.append((t, cv.resource_id, cv.characteristic_id, cv.value))

    def add_usage(self, t: float, u: CharacteristicUsage) -> None:
        self.conn.execute(
            "INSERT INTO usage_history VALUES (?,?,?,?,?,?)",
            (t, u.resource_id, u.usage_id, u.usage_type, u.value, u.unit),
        )
        self.conn.commit()

    # -------------------------------------------------------------- 조회 (재기동 복원)

    def active_fault_events(self) -> list[FaultEvent]:
        rows = self.conn.execute(
            "SELECT payload FROM fault_event WHERE state = 'ACTIVE' ORDER BY event_id"
        ).fetchall()
        return [decode(FaultEvent, r[0].encode("utf-8")) for r in rows]

    def active_threshold_events(self) -> list[ThresholdExceedanceEvent]:
        rows = self.conn.execute(
            "SELECT payload FROM threshold_event WHERE state = 'EXCEEDED' ORDER BY event_id"
        ).fetchall()
        return [decode(ThresholdExceedanceEvent, r[0].encode("utf-8")) for r in rows]

    def has_event(self, event_id: str) -> bool:
        q = "SELECT 1 FROM fault_event WHERE event_id = ? UNION SELECT 1 FROM threshold_event WHERE event_id = ?"
        return self.conn.execute(q, (event_id, event_id)).fetchone() is not None


def read_event_ledger(path: str) -> set[tuple[str, str, str]]:
    """DB에 기록된 이벤트 상태 전이 집합 {(topic, event_id, tag)} — SC5 비교용."""
    conn = sqlite3.connect(path)
    try:
        return set(conn.execute("SELECT DISTINCT topic, event_id, tag FROM event_log").fetchall())
    finally:
        conn.close()


def dump_summary(path: str) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        tables = ("resource", "spec_adoption", "fault_event", "threshold_event", "event_log",
                  "health_log", "characteristic_sample", "usage_history", "service_log", "deviation_log")
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()

