"""VCU 비휘발 메모리 모사.

블록 단위로 CRC32를 함께 저장한다. VCU 재기동 후에도 내용이 유지되도록
VCU 소프트웨어가 아닌 하드웨어(시험대) 쪽 객체로 둔다.
"""

from __future__ import annotations

import json
import zlib
from typing import Any


class NvmStore:
    def __init__(self) -> None:
        self._blocks: dict[str, tuple[bytes, int]] = {}

    def write(self, name: str, obj: Any) -> None:
        data = json.dumps(obj, sort_keys=True).encode("utf-8")
        self._blocks[name] = (data, zlib.crc32(data))

    def read(self, name: str) -> tuple[Any, bool]:
        """(내용, CRC 일치 여부). 기록된 적 없는 블록은 (None, True)."""
        if name not in self._blocks:
            return None, True
        data, crc = self._blocks[name]
        if zlib.crc32(data) != crc:
            return None, False
        return json.loads(data), True

    def corrupt(self, name: str) -> None:
        """FI-06: 블록 내용 1바이트를 손상시킨다 (CRC는 그대로 두어 불일치 유발)."""
        data, crc = self._blocks[name]
        damaged = bytes([data[0] ^ 0xFF]) + data[1:]
        self._blocks[name] = (damaged, crc)
