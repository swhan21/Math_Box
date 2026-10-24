"""편차 기록부 (설계 방침 5: 규격과 다르게 구현한 부분은 모두 기록).

UCMS 기동 시 SQLite deviation_log 테이블에 기록되고, 시험 실행 시 deviation_log.md 로도 출력된다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Deviation:
    id: str
    item: str
    reference: str
    implementation: str
    reason: str


DEVIATIONS: tuple[Deviation, ...] = (
    Deviation(
        "DEV-001", "토픽 데이터 타입",
        "6.1절 생성 코드 원칙 (IDL → 코드 생성기)",
        "topics.py 의 자체 정의 데이터클래스 (토픽 이름만 공개 문서 기준, 필드는 최소 정의)",
        "LDM/NGVA IDL 미확보 가정 (R2). 원문 IDL 확보 시 생성 코드로 교체",
    ),
    Deviation(
        "DEV-002", "미들웨어",
        "OMG DDS / DDSI-RTPS (4장)",
        "프로세스 내 모의 DDS (durability·reliability·history·lifespan·QoS 호환성만 모사)",
        "SIL 단계 (8.1절 T1·T2). T3 이후 실제 DDS 구현으로 교체",
    ),
    Deviation(
        "DEV-003", "리소스 등록",
        "GVA Registry Service 등록 (부록 A.4.3)",
        "정적 ID + 자체 정의 Resource_Registration 토픽",
        "D7 권장안 (간이 정적 ID)",
    ),
    Deviation(
        "DEV-004", "건전성 집계 규칙",
        "규격에 집계 알고리즘 없음",
        "PBIT 실패 또는 심각도 high 고장(래치 포함) 활성 시 RED, 그 외 GREEN",
        "7.4절 규칙을 ICD에 명시",
    ),
    Deviation(
        "DEV-005", "래치 고장 운용자 해제",
        "정비조치·O-role 명령 토픽 (원문 미확보)",
        "자체 정의 명령 토픽 Fault_Code__clearFault (VOLATILE, 수명 10 s)",
        "6.5절 상태도의 '운용자 해제' 전이를 구현하기 위함",
    ),
    Deviation(
        "DEV-006", "이벤트 인스턴스 수명",
        "Fault_Event / Threshold_Exceedance_Event 수명 규정 미확인",
        "해제 시 해제 상태를 발행한 뒤 인스턴스 dispose → 늦은 접속자는 활성 이벤트만 수신 (이력은 DB)",
        "TRANSIENT_LOCAL 이력 무한 증가 방지",
    ),
    Deviation(
        "DEV-007", "QoS 설정 형식",
        "6.7절: 기동 시 XML 설정 파일",
        "config/qos.yaml",
        "모의 DDS 한정. 실제 DDS 도입 시 XML로 변환",
    ),
    Deviation(
        "DEV-008", "특성값 사양 토픽",
        "Monitored_Attribute/Average/Event_Count/Powered_On/Time_Specification (부록 A.4.5)",
        "Monitored_Attribute_Specification 하나에 상한·하한·히스테리시스·attack/release 표현",
        "LDM 필드 구조 미확인",
    ),
    Deviation(
        "DEV-009", "임계값 해제 조건",
        "6.5절 상태도 (횟수 기반 attack/release)",
        "횟수 기반 + 값 기반 불감대(hysteresis): 해제 판정은 한계값에서 불감대만큼 안쪽",
        "임계값 근처 진동 시 이벤트 난발 억제 (FI-07)",
    ),
    Deviation(
        "DEV-010", "지연 측정 시계",
        "5.4절: 하니스 PC 시계로 t0·t1 기록",
        "모든 노드가 공유하는 모의 시계로 t0·t1 기록",
        "SIL에서는 단일 시계라 노드 간 동기 오차 없음 (측정 원리 동일)",
    ),
    Deviation(
        "DEV-011", "고장코드 사양 미수신 처리",
        "규정 미확인",
        "Fault_Code를 사양 수신 시까지 보류, 1 s 내 미수신 시 UNKNOWN 사양으로 이벤트 생성",
        "토픽 간 수신 순서가 보장되지 않음 (DDS 특성)",
    ),
)


def to_markdown() -> str:
    lines = [
        "# 편차 기록부 (Deviation Log)",
        "",
        "규격·설계안 대비 시뮬레이션 구현의 차이와 사유. 원문 규격 확보 시 재점검 필요.",
        "",
        "| ID | 항목 | 기준 | 구현 | 사유 |",
        "|---|---|---|---|---|",
    ]
    for d in DEVIATIONS:
        lines.append(f"| {d.id} | {d.item} | {d.reference} | {d.implementation} | {d.reason} |")
    return "\n".join(lines) + "\n"
