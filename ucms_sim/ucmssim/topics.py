"""UCM 토픽 타입 정의 (자체 정의 — 편차 DEV-001).

LDM 10 / NGVA 데이터 모델 UCM 모듈의 IDL을 확보하지 못한 상태(위험 R2)를 가정하여,
공개 문서(부록 A.4.5)의 토픽 이름에 최소 필드만 붙인 데이터클래스로 정의한다.
원문 IDL 확보 시 IDL → 코드 생성기 산출물로 교체한다 (설계 원칙 6.1 '생성 코드').

모든 타입은 평면(flat) 구조이며 JSON으로 직렬화되어 모의 DDS 위를 오간다.
"""

from __future__ import annotations

import json
import typing
from dataclasses import dataclass, fields
from enum import Enum
from typing import Any


class Health(str, Enum):
    """리소스 건전성 (7.4절 규칙: Red/Green 2값)."""

    GREEN = "GREEN"
    RED = "RED"


class BitType(str, Enum):
    PBIT = "PBIT"
    CBIT = "CBIT"
    IBIT = "IBIT"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ClearPolicy(str, Enum):
    """고장 해제 방식 (7.1절 '해제' 열)."""

    AUTO = "auto"
    LATCH = "latch"
    RESTART = "restart"


class FaultState(str, Enum):
    """고장코드 상태 (6.5절 상태도)."""

    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    LATCHED = "LATCHED"


class EventState(str, Enum):
    ACTIVE = "ACTIVE"
    CLEARED = "CLEARED"


class ExceedanceState(str, Enum):
    EXCEEDED = "EXCEEDED"
    RELEASED = "RELEASED"


class LimitType(str, Enum):
    MAX = "MAX"
    MIN = "MIN"


class SpecProvider(str, Enum):
    """감시 사양 발행 주체 (리소스 자신 또는 브리지)."""

    RESOURCE = "RESOURCE"
    BRIDGE = "BRIDGE"


TOPICS: dict[str, type] = {}


def topic(name: str, key: tuple[str, ...]):
    """데이터클래스를 토픽 타입으로 등록한다. key = DDS 키 필드(인스턴스 식별)."""

    def register(cls):
        cls = dataclass(frozen=True)(cls)
        cls.TOPIC = name
        cls.KEY = key
        TOPICS[name] = cls
        return cls

    return register


# ---------------------------------------------------------------- 리소스(R-role) 발행


@topic("Resource_Registration", key=("resource_id",))
class ResourceRegistration:
    """간이 등록 (D7: 정적 ID, 편차 DEV-003). 정식 GVA Registry 토픽 아님."""

    resource_id: str
    name: str
    kind: str
    boot_count: int
    timestamp: float


@topic("Resource_With_Monitoring", key=("resource_id",))
class ResourceWithMonitoring:
    """리소스 자체 판정 건전성 + PBIT 결과 (필수, §4.1.2)."""

    resource_id: str
    health: Health
    pbit_complete: bool
    pbit_passed: bool
    active_fault_count: int
    timestamp: float


@topic("Monitored_Resource_Specification", key=("resource_id",))
class MonitoredResourceSpecification:
    resource_id: str
    name: str
    description: str
    provider: SpecProvider
    rationale: str


@topic("Monitored_Attribute_Specification", key=("resource_id", "characteristic_id"))
class MonitoredAttributeSpecification:
    """특성값 사양 + 물리 한계 임계값 (편차 DEV-008: 상·하한을 한 토픽에 표현)."""

    resource_id: str
    characteristic_id: str
    name: str
    unit: str
    rate_hz: float
    max_limit: float | None
    min_limit: float | None
    hysteresis: float
    attack_samples: int
    release_samples: int
    provider: SpecProvider
    rationale: str


@topic("Monitored_Characteristic__supplyCharacteristicValue", key=("resource_id", "characteristic_id"))
class CharacteristicValue:
    resource_id: str
    characteristic_id: str
    value: float
    timestamp: float


@topic("Fault_Code_Specification", key=("resource_id", "fault_code_id"))
class FaultCodeSpecification:
    resource_id: str
    fault_code_id: str
    internal_dtc: int
    description: str
    bit_type: BitType
    severity: Severity
    clear_policy: ClearPolicy


@topic("Fault_Code", key=("resource_id", "fault_code_id"))
class FaultCode:
    resource_id: str
    fault_code_id: str
    state: FaultState
    bit_type: BitType
    timestamp: float          # BIT 검출(또는 해소) 시각


@topic("Characteristic_Usage", key=("resource_id", "usage_id"))
class CharacteristicUsage:
    resource_id: str
    usage_id: str
    usage_type: str
    value: float
    unit: str
    timestamp: float


# ---------------------------------------------------------------- O-role(HMI) 명령


@topic("Fault_Code__clearFault", key=("resource_id", "fault_code_id"))
class FaultClearCommand:
    """래치 고장 운용자 해제 명령 (자체 정의, 편차 DEV-005)."""

    resource_id: str
    fault_code_id: str
    operator: str
    timestamp: float


# ---------------------------------------------------------------- UCMS 발행


@topic("Monitored_Resource", key=("resource_id",))
class MonitoredResource:
    """리소스 판정값을 UCMS가 그대로 재발행한 것."""

    resource_id: str
    health: Health
    pbit_complete: bool
    pbit_passed: bool
    source_timestamp: float
    timestamp: float


@topic("Fault_Event", key=("event_id",))
class FaultEvent:
    event_id: str
    resource_id: str
    fault_code_id: str
    description: str
    severity: str
    bit_type: BitType
    state: EventState
    latched: bool
    raised_time: float
    cleared_time: float | None
    source_timestamp: float


@topic("Threshold_Exceedance_Event", key=("event_id",))
class ThresholdExceedanceEvent:
    event_id: str
    resource_id: str
    characteristic_id: str
    limit_type: LimitType
    limit_value: float
    trigger_value: float
    release_value: float | None
    state: ExceedanceState
    spec_provider: SpecProvider
    exceeded_time: float
    released_time: float | None
    source_timestamp: float


# ---------------------------------------------------------------- 직렬화

_FIELDS: dict[type, tuple[str, ...]] = {}
_ENUM_FIELDS: dict[type, dict[str, type]] = {}


def _field_names(cls: type) -> tuple[str, ...]:
    names = _FIELDS.get(cls)
    if names is None:
        names = _FIELDS[cls] = tuple(f.name for f in fields(cls))
        hints = typing.get_type_hints(cls)
        _ENUM_FIELDS[cls] = {
            n: h for n, h in hints.items() if isinstance(h, type) and issubclass(h, Enum)
        }
    return names


def key_of(sample: Any) -> tuple:
    return tuple(getattr(sample, k) for k in type(sample).KEY)


def to_dict(sample: Any) -> dict[str, Any]:
    return {n: getattr(sample, n) for n in _field_names(type(sample))}


def to_plain_dict(sample: Any) -> dict[str, Any]:
    """Enum 을 값 문자열로 바꾼 사전 (화면 표시·JSON용)."""
    return {k: v.value if isinstance(v, Enum) else v for k, v in to_dict(sample).items()}


def encode(sample: Any) -> bytes:
    """온와이어 표현 (모의 DDS 직렬화). str 기반 Enum은 값 문자열로 직렬화된다."""
    return json.dumps(to_dict(sample), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode(cls: type, payload: bytes) -> Any:
    names = _field_names(cls)
    enums = _ENUM_FIELDS[cls]
    data = json.loads(payload)
    kwargs = {}
    for n in names:
        v = data[n]
        if v is not None and n in enums:
            v = enums[n](v)
        kwargs[n] = v
    return cls(**kwargs)


def decode_raw(payload: bytes) -> dict[str, Any]:
    """타입 정보 없이 온와이어 표현만 해석한다 (이종 구현 구독자 모사용)."""
    return json.loads(payload)


# ---------------------------------------------------------------- 이벤트 원장 태그 (SC5)


def fault_event_tag(ev: FaultEvent) -> str:
    if ev.state is EventState.CLEARED:
        return "CLEARED"
    return "LATCHED" if ev.latched else "ACTIVE"


def threshold_event_tag(ev: ThresholdExceedanceEvent) -> str:
    return ev.state.value
