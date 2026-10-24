"""설정 파일 로더 (설계 원칙 '설정 기반': 고장 매핑·특성값·임계값·건전성 규칙은 YAML).

값이 TBD로 남아 있으면 ConfigError를 낸다. 연구설계안의 TBD 항목은 착수 시 확정해야 하며,
시뮬레이션에는 config/*.yaml 의 예시값을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml

from .dds import Durability, LatencyModel, Qos, Reliability
from .plant import PlantParams
from .topics import TOPICS, BitType, ClearPolicy, Health, Severity
from .vcu.control import BitCalibration, ControlGains

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

_REQUIRED = object()


class ConfigError(ValueError):
    pass


def _get(d: Any, key: str, where: str, conv=None, default: Any = _REQUIRED) -> Any:
    if not isinstance(d, dict):
        raise ConfigError(f"{where}: 매핑(dict)이어야 함")
    if key not in d or d[key] is None:
        if default is _REQUIRED:
            raise ConfigError(f"{where}.{key}: 누락")
        return default
    v = d[key]
    if isinstance(v, str) and v.strip().upper() == "TBD":
        raise ConfigError(f"{where}.{key} = TBD — 착수 시 확정 필요 (시뮬레이션에는 예시값을 입력)")
    if conv is None:
        return v
    try:
        return conv(v)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{where}.{key}: 값 오류 ({v!r}): {e}") from None


def _float_dataclass(cls: type, d: dict | None, where: str):
    d = d or {}
    names = {f.name for f in fields(cls)}
    unknown = set(d) - names
    if unknown:
        raise ConfigError(f"{where}: 알 수 없는 항목 {sorted(unknown)}")
    return cls(**{k: _get(d, k, where, float) for k in d})


# ---------------------------------------------------------------- VCU UCM 설정


@dataclass(frozen=True)
class ThresholdDef:
    max: float | None = None
    min: float | None = None
    hysteresis: float = 0.0
    attack_samples: int = 1
    release_samples: int = 1


@dataclass(frozen=True)
class FaultDef:
    id: str
    internal_dtc: int
    description: str
    bit_type: BitType
    severity: Severity
    clear: ClearPolicy


@dataclass(frozen=True)
class UnmappedDtc:
    internal_dtc: int
    reason: str


@dataclass(frozen=True)
class CharacteristicDef:
    id: str
    name: str
    unit: str
    rate_hz: float
    signal: str
    threshold: ThresholdDef | None


@dataclass(frozen=True)
class UsageDef:
    id: str
    type: str
    unit: str
    publish_period_s: float | None
    persist: bool


@dataclass(frozen=True)
class HealthCondition:
    kind: str                       # pbit_failed | any_active_fault_with_severity
    severity: Severity | None = None


@dataclass(frozen=True)
class HealthRule:
    red_if: tuple[HealthCondition, ...]
    otherwise: Health


@dataclass(frozen=True)
class VcuUcmConfig:
    resource_id: str
    name: str
    kind: str
    task_period_s: float
    queue_size: int
    faults: tuple[FaultDef, ...]
    unmapped: tuple[UnmappedDtc, ...]
    characteristics: tuple[CharacteristicDef, ...]
    usage: tuple[UsageDef, ...]
    health_rule: HealthRule

    def fault(self, fault_id: str) -> FaultDef:
        return next(f for f in self.faults if f.id == fault_id)

    def characteristic(self, char_id: str) -> CharacteristicDef:
        return next(c for c in self.characteristics if c.id == char_id)


def _threshold(d: Any, where: str) -> ThresholdDef | None:
    if d is None:
        return None
    th = ThresholdDef(
        max=_get(d, "max", where, float, None),
        min=_get(d, "min", where, float, None),
        hysteresis=_get(d, "hysteresis", where, float, 0.0),
        attack_samples=_get(d, "attack_samples", where, int, 1),
        release_samples=_get(d, "release_samples", where, int, 1),
    )
    if th.max is None and th.min is None:
        raise ConfigError(f"{where}: max 또는 min 중 하나는 필요")
    if th.attack_samples < 1 or th.release_samples < 1:
        raise ConfigError(f"{where}: attack/release_samples >= 1")
    return th


def _health_rule(d: Any, where: str) -> HealthRule:
    conds = []
    for i, item in enumerate(_get(d, "red_if", where)):
        w = f"{where}.red_if[{i}]"
        if item == "pbit_failed":
            conds.append(HealthCondition("pbit_failed"))
        elif isinstance(item, dict) and "any_active_fault_with_severity" in item:
            conds.append(HealthCondition(
                "any_active_fault_with_severity",
                _get(item, "any_active_fault_with_severity", w, Severity),
            ))
        else:
            raise ConfigError(f"{w}: 알 수 없는 조건 {item!r}")
    otherwise = _get(d, "otherwise", where, lambda v: Health(str(v).upper()))
    return HealthRule(tuple(conds), otherwise)


def load_vcu_config(path: Path) -> VcuUcmConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    res = _get(raw, "resource", "vcu_ucm")
    adapter = _get(raw, "adapter", "vcu_ucm")

    faults = []
    for i, f in enumerate(_get(raw, "faults", "vcu_ucm")):
        w = f"faults[{i}]"
        faults.append(FaultDef(
            id=_get(f, "id", w, str),
            internal_dtc=_get(f, "internal_dtc", w, int),
            description=_get(f, "description", w, str),
            bit_type=_get(f, "bit_type", w, BitType),
            severity=_get(f, "severity", w, Severity),
            clear=_get(f, "clear", w, ClearPolicy),
        ))
    unmapped = tuple(
        UnmappedDtc(_get(u, "internal_dtc", f"unmapped_dtcs[{i}]", int), _get(u, "reason", f"unmapped_dtcs[{i}]", str))
        for i, u in enumerate(raw.get("unmapped_dtcs") or [])
    )
    chars = []
    for i, c in enumerate(_get(raw, "characteristics", "vcu_ucm")):
        w = f"characteristics[{i}]"
        chars.append(CharacteristicDef(
            id=_get(c, "id", w, str),
            name=_get(c, "name", w, str),
            unit=_get(c, "unit", w, str),
            rate_hz=_get(c, "rate_hz", w, float),
            signal=_get(c, "signal", w, str),
            threshold=_threshold(c.get("threshold"), f"{w}.threshold"),
        ))
    usage = []
    for i, u in enumerate(raw.get("usage") or []):
        w = f"usage[{i}]"
        usage.append(UsageDef(
            id=_get(u, "id", w, str),
            type=_get(u, "type", w, str),
            unit=_get(u, "unit", w, str),
            publish_period_s=_get(u, "publish_period_s", w, float, None),
            persist=_get(u, "persist", w, bool, False),
        ))

    cfg = VcuUcmConfig(
        resource_id=_get(res, "id", "resource", str),
        name=_get(res, "name", "resource", str),
        kind=_get(res, "kind", "resource", str, "LRU"),
        task_period_s=_get(adapter, "task_period_ms", "adapter", float) / 1000.0,
        queue_size=_get(adapter, "queue_size", "adapter", int, 256),
        faults=tuple(faults),
        unmapped=unmapped,
        characteristics=tuple(chars),
        usage=tuple(usage),
        health_rule=_health_rule(_get(raw, "health_rule", "vcu_ucm"), "health_rule"),
    )
    _check_unique("faults.id", [f.id for f in cfg.faults])
    _check_unique("faults.internal_dtc", [f.internal_dtc for f in cfg.faults] + [u.internal_dtc for u in cfg.unmapped])
    _check_unique("characteristics.id", [c.id for c in cfg.characteristics])
    _check_unique("usage.id", [u.id for u in cfg.usage])
    return cfg


def _check_unique(what: str, values: list) -> None:
    dup = {v for v in values if values.count(v) > 1}
    if dup:
        raise ConfigError(f"{what} 중복: {sorted(dup)}")


# ---------------------------------------------------------------- 브리지 설정


@dataclass(frozen=True)
class BridgeFallback:
    resource: str
    characteristic: str
    name: str
    unit: str
    rate_hz: float
    threshold: ThresholdDef
    rationale: str


@dataclass(frozen=True)
class BridgeConfig:
    grace_s: float
    fallbacks: tuple[BridgeFallback, ...]


def load_bridge_config(path: Path) -> BridgeConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    policy = _get(raw, "policy", "ucms_bridge")
    fallbacks = []
    for i, f in enumerate(raw.get("fallbacks") or []):
        w = f"fallbacks[{i}]"
        char = _get(f, "characteristic", w, str)
        fallbacks.append(BridgeFallback(
            resource=_get(f, "resource", w, str),
            characteristic=char,
            name=_get(f, "name", w, str, char),
            unit=_get(f, "unit", w, str),
            rate_hz=_get(f, "rate_hz", w, float, 1.0),
            threshold=_threshold(_get(f, "threshold", w), f"{w}.threshold"),
            rationale=_get(f, "rationale", w, str),
        ))
    return BridgeConfig(_get(policy, "bridge_grace_ms", "policy", float) / 1000.0, tuple(fallbacks))


# ---------------------------------------------------------------- QoS 설정


class QosConfig:
    def __init__(self, profiles: dict[str, Qos], topic_profiles: dict[str, str]) -> None:
        self.profiles = profiles
        self.topic_profiles = topic_profiles

    def for_topic(self, topic: str) -> Qos:
        try:
            return self.profiles[self.topic_profiles[topic]]
        except KeyError:
            raise ConfigError(f"QoS 미정의 토픽: {topic}") from None


def load_qos_config(path: Path) -> QosConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    profiles = {}
    for name, p in _get(raw, "profiles", "qos").items():
        w = f"profiles.{name}"
        profiles[name] = Qos(
            durability=_get(p, "durability", w, Durability),
            reliability=_get(p, "reliability", w, Reliability),
            depth=_get(p, "depth", w, int, 1),
            lifespan_s=_get(p, "lifespan_s", w, float, None),
        )
    topic_profiles = dict(_get(raw, "topics", "qos"))
    for topic, prof in topic_profiles.items():
        if topic not in TOPICS:
            raise ConfigError(f"qos.topics: 알 수 없는 토픽 {topic}")
        if prof not in profiles:
            raise ConfigError(f"qos.topics.{topic}: 알 수 없는 프로파일 {prof}")
    missing = sorted(set(TOPICS) - set(topic_profiles))
    if missing:
        raise ConfigError(f"qos.topics: QoS 미정의 토픽 {missing}")
    return QosConfig(profiles, topic_profiles)


# ---------------------------------------------------------------- 시뮬레이션 환경 설정


@dataclass(frozen=True)
class VcuTiming:
    boot_s: float
    pbit_s: float
    control_period_s: float
    control_phase_s: float


@dataclass(frozen=True)
class UcmsSettings:
    processing_delay_s: float
    sample_commit_period_s: float
    fault_spec_wait_s: float


@dataclass(frozen=True)
class HarnessSettings:
    repetitions: int
    latency_limit_s: float
    latency_percentile: float
    fi11_duration_s: float
    settle_s: float


@dataclass(frozen=True)
class SimConfig:
    plant_step_s: float
    latency: LatencyModel
    plant: PlantParams
    bit: BitCalibration
    gains: ControlGains
    vcu: VcuTiming
    ucms: UcmsSettings
    harness: HarnessSettings


def load_sim_config(path: Path) -> SimConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    net = _get(raw, "network", "sim")
    vcu = _get(raw, "vcu", "sim")
    ctl = _get(raw, "vcu_control", "sim")
    ucms = _get(raw, "ucms", "sim")
    har = _get(raw, "harness", "sim")
    return SimConfig(
        plant_step_s=_get(raw, "plant_step_ms", "sim", float) / 1000.0,
        latency=LatencyModel(
            min_s=_get(net, "latency_min_ms", "network", float) / 1000.0,
            max_s=_get(net, "latency_max_ms", "network", float) / 1000.0,
            best_effort_loss=_get(net, "best_effort_loss", "network", float, 0.0),
        ),
        plant=_float_dataclass(PlantParams, raw.get("plant"), "plant"),
        bit=_float_dataclass(BitCalibration, raw.get("vcu_bit"), "vcu_bit"),
        gains=ControlGains(
            kp=_get(ctl, "kp", "vcu_control", float),
            ki=_get(ctl, "ki", "vcu_control", float),
            max_torque_nm=_get(ctl, "max_torque_nm", "vcu_control", float),
            max_regen_nm=_get(ctl, "max_regen_nm", "vcu_control", float),
        ),
        vcu=VcuTiming(
            boot_s=_get(vcu, "boot_ms", "vcu", float) / 1000.0,
            pbit_s=_get(vcu, "pbit_ms", "vcu", float) / 1000.0,
            control_period_s=_get(vcu, "control_period_ms", "vcu", float) / 1000.0,
            control_phase_s=_get(vcu, "control_phase_ms", "vcu", float, 0.0) / 1000.0,
        ),
        ucms=UcmsSettings(
            processing_delay_s=_get(ucms, "processing_delay_ms", "ucms", float) / 1000.0,
            sample_commit_period_s=_get(ucms, "sample_commit_period_s", "ucms", float),
            fault_spec_wait_s=_get(ucms, "fault_spec_wait_s", "ucms", float),
        ),
        harness=HarnessSettings(
            repetitions=_get(har, "repetitions", "harness", int),
            latency_limit_s=_get(har, "latency_limit_s", "harness", float),
            latency_percentile=_get(har, "latency_percentile", "harness", float),
            fi11_duration_s=_get(har, "fi11_duration_s", "harness", float),
            settle_s=_get(har, "settle_s", "harness", float),
        ),
    )


# ---------------------------------------------------------------- 전체


@dataclass(frozen=True)
class Configs:
    vcu: VcuUcmConfig
    bridge: BridgeConfig
    qos: QosConfig
    sim: SimConfig
    source_dir: Path


def load_configs(config_dir: str | Path | None = None) -> Configs:
    d = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    return Configs(
        vcu=load_vcu_config(d / "vcu_ucm.yaml"),
        bridge=load_bridge_config(d / "ucms_bridge.yaml"),
        qos=load_qos_config(d / "qos.yaml"),
        sim=load_sim_config(d / "sim.yaml"),
        source_dir=d,
    )
