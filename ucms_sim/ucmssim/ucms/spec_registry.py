"""Spec Registry: 리소스 발행 사양 + 브리지 사양 관리 (설계안 6.4절, 부록 A.6).

브리지 처리 순서
  1. 기동 시 브리지 설정 로딩
  2. 대기 시간(기본 5 s) 동안 리소스의 사양 발행을 기다림
  3. 리소스가 발행하면 리소스 사양 채택, 아니면 브리지 사양 발행
  4. 채택 결과와 근거를 로그(DB spec_adoption)로 기록
대기 시간 이후에 리소스 사양이 도착해도 리소스 사양으로 교체한다 (리소스 발행 우선).
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import BridgeConfig, BridgeFallback
from ..topics import (
    FaultCodeSpecification,
    MonitoredAttributeSpecification,
    MonitoredResourceSpecification,
    SpecProvider,
)
from .persistence import UcmsStore

AttrKey = tuple[str, str]


class SpecRegistry:
    def __init__(
        self,
        bridge: BridgeConfig,
        now: Callable[[], float],
        store: UcmsStore,
        publish: Callable[[Any], None],
    ) -> None:
        self.bridge = bridge
        self.now = now
        self.store = store
        self.publish = publish
        self.grace_expired = False
        self._attr: dict[AttrKey, tuple[MonitoredAttributeSpecification, SpecProvider]] = {}
        self._fault: dict[AttrKey, FaultCodeSpecification] = {}
        self._resource: dict[str, MonitoredResourceSpecification] = {}

    # -------------------------------------------------------------- 조회

    def attribute_spec(self, resource_id: str, characteristic_id: str):
        """(사양, 채택 출처) 또는 None."""
        return self._attr.get((resource_id, characteristic_id))

    def fault_spec(self, resource_id: str, fault_code_id: str) -> FaultCodeSpecification | None:
        return self._fault.get((resource_id, fault_code_id))

    def resource_spec(self, resource_id: str) -> MonitoredResourceSpecification | None:
        return self._resource.get(resource_id)

    # -------------------------------------------------------------- 수신

    def on_resource_spec(self, spec: MonitoredResourceSpecification) -> None:
        self._resource[spec.resource_id] = spec

    def on_fault_spec(self, spec: FaultCodeSpecification) -> None:
        self._fault[(spec.resource_id, spec.fault_code_id)] = spec

    def on_attribute_spec(self, spec: MonitoredAttributeSpecification) -> None:
        key = (spec.resource_id, spec.characteristic_id)
        cur = self._attr.get(key)
        if cur is not None and cur[0] == spec:
            return                                      # 재수신 (재기동·늦은 접속 재생)
        if spec.provider is SpecProvider.RESOURCE:
            if cur is not None and cur[1] is SpecProvider.BRIDGE:
                reason = "리소스 사양 수신 → 브리지 사양 대체 (리소스 발행 우선)"
            elif cur is None:
                reason = "리소스 발행 사양 채택"
            else:
                reason = "리소스 사양 갱신"
            self._adopt(key, spec, SpecProvider.RESOURCE, reason)
        else:                                           # 별도 개체로 구현된 브리지가 발행한 사양
            if cur is not None and cur[1] is SpecProvider.RESOURCE:
                return
            self._adopt(key, spec, SpecProvider.BRIDGE, f"외부 브리지 사양 채택: {spec.rationale}")

    # -------------------------------------------------------------- 브리지

    def expire_grace(self) -> None:
        self.grace_expired = True
        for fb in self.bridge.fallbacks:
            key = (fb.resource, fb.characteristic)
            cur = self._attr.get(key)
            if cur is not None:
                self.store.log_spec_adoption(
                    self.now(), fb.resource, fb.characteristic, cur[1].value,
                    "대기 시간 내 사양 수신 — 브리지 사양 미사용",
                )
                continue
            spec = self._bridge_spec(fb)
            self._adopt(
                key, spec, SpecProvider.BRIDGE,
                f"대기 {self.bridge.grace_s:g} s 내 리소스 사양 없음 → 브리지 사양 발행 (근거: {fb.rationale})",
            )
            self.publish(spec)
            if fb.resource not in self._resource:
                rspec = MonitoredResourceSpecification(
                    fb.resource, fb.resource, "브리지 대리 사양", SpecProvider.BRIDGE, fb.rationale
                )
                self._resource[fb.resource] = rspec
                self.publish(rspec)

    @staticmethod
    def _bridge_spec(fb: BridgeFallback) -> MonitoredAttributeSpecification:
        th = fb.threshold
        return MonitoredAttributeSpecification(
            resource_id=fb.resource,
            characteristic_id=fb.characteristic,
            name=fb.name,
            unit=fb.unit,
            rate_hz=fb.rate_hz,
            max_limit=th.max,
            min_limit=th.min,
            hysteresis=th.hysteresis,
            attack_samples=th.attack_samples,
            release_samples=th.release_samples,
            provider=SpecProvider.BRIDGE,
            rationale=fb.rationale,
        )

    def _adopt(
        self, key: AttrKey, spec: MonitoredAttributeSpecification, provider: SpecProvider, reason: str
    ) -> None:
        self._attr[key] = (spec, provider)
        self.store.log_spec_adoption(self.now(), key[0], key[1], provider.value, reason)
