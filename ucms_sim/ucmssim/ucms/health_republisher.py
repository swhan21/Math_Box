"""Health Republisher: Resource_With_Monitoring → Monitored_Resource (설계안 6.4절).

건전성 값은 리소스가 스스로 판정하며 UCMS는 그대로 재발행한다 (부록 A.4.4).
DB에는 상태가 바뀐 경우만 기록한다.
"""

from __future__ import annotations

from typing import Any, Callable

from ..topics import MonitoredResource, ResourceWithMonitoring
from .persistence import UcmsStore


class HealthRepublisher:
    def __init__(self, store: UcmsStore, publish: Callable[[Any], None], now: Callable[[], float]) -> None:
        self.store = store
        self.publish = publish
        self.now = now
        self.latest: dict[str, MonitoredResource] = {}

    def on_resource_with_monitoring(self, rwm: ResourceWithMonitoring) -> None:
        t = self.now()
        mr = MonitoredResource(rwm.resource_id, rwm.health, rwm.pbit_complete, rwm.pbit_passed, rwm.timestamp, t)
        prev = self.latest.get(rwm.resource_id)
        self.latest[rwm.resource_id] = mr
        if prev is None or (prev.health, prev.pbit_complete, prev.pbit_passed) != (
            mr.health, mr.pbit_complete, mr.pbit_passed
        ):
            self.store.log_health(t, mr)
        self.publish(mr)
