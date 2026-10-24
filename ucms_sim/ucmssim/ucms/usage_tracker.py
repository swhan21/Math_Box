"""Usage Tracker: Characteristic_Usage 이력 기록 (설계안 6.4절, 2단계 기능)."""

from __future__ import annotations

from typing import Callable

from ..topics import CharacteristicUsage
from .persistence import UcmsStore


class UsageTracker:
    def __init__(self, store: UcmsStore, now: Callable[[], float]) -> None:
        self.store = store
        self.now = now
        self.latest: dict[tuple[str, str], CharacteristicUsage] = {}

    def on_usage(self, u: CharacteristicUsage) -> None:
        self.latest[(u.resource_id, u.usage_id)] = u
        self.store.add_usage(self.now(), u)
