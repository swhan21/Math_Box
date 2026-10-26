# UCMS SIL 시뮬레이터 (UGV 차량제어기 대상 UCMS 개념검증)

「UGV 차량제어기 대상 UCMS 개념검증(PoC) 연구설계안」의 **SIL 선행**(설계 방침 2) 단계를 위한 코드다.
설계안 8.1절의 T1(단위시험)·T2(SIL 통합) 단계를 PC 한 대에서 수행한다.

HILS 장비 없이 다음을 하나의 모의 시계 위에서 함께 돌린다.

- 플랜트 모델 (구동모터·인버터·배터리·센서) + 고장 주입 블록
- 기존 VCU 제어 SW·BIT (모사) + **UCM 어댑터 (R-role)**
- 단순 장비 1종 (R-role 미지원 → 브리지 검증)
- **최소 UCMS** (건전성 재발행, 고장 이벤트 도출, 임계값 판정, SQLite 저장, 브리지)
- 간이 HMI (O-role 일부), 시험 하니스 (FI-01~FI-13, 지연 측정, SC1~SC9 판정)

> 모든 수치(임계값·디바운스·플랜트 파라미터)는 시뮬레이션용 **예시값**이다. 설계안의 TBD 항목은
> 착수 시 확정해 `config/*.yaml` 에 넣는다. 값을 `TBD` 로 두면 로더가 오류를 낸다.

## 실행

Python 3.10 이상 (3.12·3.13에서 시험), 외부 의존성은 PyYAML 하나. HMI 화면(`gui`)은 표준 tkinter를 쓴다.

```bash
cd ucms_sim
pip install -r requirements.txt

python -m ucmssim list                   # 시나리오 목록
python -m ucmssim run --quick            # 빠른 확인 (반복 3회, FI-11 5분) — 수 초
python -m ucmssim run                    # 전체 (FI-01~03 각 30회, FI-11 1시간) — 약 25 s
python -m ucmssim run --scenarios FI-01,FI-08 --reps 10 --out out/partial
python -m ucmssim demo                   # 서술형 데모 (HMI 화면 출력)
python -m ucmssim demo --realtime 5 --live   # 5배속, HMI 화면 1초마다 갱신
python -m ucmssim gui --speed 5              # 간이 HMI 화면 (tkinter 창)

python -m unittest discover -s tests -t .    # 단위·통합 시험
```

`run` 의 출력 (`out/` 기본)

| 파일 | 내용 |
|---|---|
| `summary.md` | SC1~SC9 판정, 지연 통계(p50/p95/최대), 시나리오별 결과, 진단 항목 매핑표 |
| `results.csv` | 실행별 시나리오 ID, t0, t1, 지연, 이벤트, DB 기록 여부 (설계안 8.3절) |
| `db/FI-xx.sqlite` | 시나리오별 UCMS 저장소 (마지막 실행) |
| `deviation_log.md` | 편차 기록부 (DEV-001~011) |
| `hmi/*.txt` | 간이 HMI 화면 스냅샷 (FI-09 늦은 접속 화면 등) |

## 간이 HMI 화면 (GUI)

```bash
python -m ucmssim gui                        # 실시간(1×)으로 시작
python -m ucmssim gui --speed 5 --theme light
```

시뮬레이션을 실시간(배속 조절 가능)으로 돌리면서 HMI 창을 띄운다. 설계안 6.8절의 "Python GUI" 안이며
표준 라이브러리 tkinter만 쓴다. Windows·macOS용 공식 Python에는 tkinter가 포함되어 있고,
Ubuntu는 `sudo apt install python3-tk` 가 필요하다. 그래프도 tkinter 캔버스로 그리므로 matplotlib 등은 필요 없다.

| 영역 | 내용 |
|---|---|
| 리소스 건전성 | `Monitored_Resource`: 정상(GREEN)/고장(RED), PBIT 결과, 활성 고장 수. 브리지 장비는 "R-role 미지원" |
| 활성 고장 | `Fault_Event`: 심각도·코드·내용·BIT 유형·활성/래치·발생 시각. 래치 고장을 고르고 **[선택한 래치 고장 해제]** → `Fault_Code__clearFault` 송신 (래치 고장이 하나면 자동 선택) |
| 임계값 초과 | `Threshold_Exceedance_Event`: 상한 초과/하한 미달, 리소스/특성, 한계·초과 값 |
| 특성값 추이 | 최근 3분 차트(점선 = 임계값, 넘는 쪽 음영), 커서를 올리면 값·시각 표시. [표로 보기]로 표 전환(사양 출처 포함) |
| 사용량·이벤트 기록 | U01 가동시간, U02 비상정지 횟수, HMI가 받은 순서의 이벤트 |
| 시험 하니스 (오른쪽) | HMI 기능이 아닌 시험용 패널: 배속, 속도 지령, 고장 주입(FI-01·04·05 켜고 끄기, FI-02·03·07·10 프로파일, C03 과열), VCU·UCMS 재기동(FI-06·08), 플랜트 참값 |

화면 왼쪽은 HMI가 DDS로 실제 받은 값만 보여주고, 플랜트 참값은 하니스 패널에만 보인다.
어두운 화면이 기본이고 상단 버튼(또는 `--theme light`)으로 밝은 화면으로 바꿀 수 있다.
시뮬레이션은 창의 이벤트 루프가 50 ms마다 진행시키고(`SimRuntime.pump`), 화면은 300 ms마다 갱신한다.

## 구성

```
 시험 하니스 ─(고장 주입)→ 플랜트 모델 ─(CAN·센서)→ VCU: 기존 제어·BIT ─(단방향 채널)→ UCM 어댑터
                                                                                    │ 모의 DDS
 단순 장비 ─(값만 발행)──────────────────────────────────────→ 최소 UCMS ←───────────┘
                                                                │ 모의 DDS
                                                   간이 HMI · 하니스 구독자(t1 기록)
```

설계안 컴포넌트 ↔ 코드

| 설계안 | 모듈 | 파일 |
|---|---|---|
| 5.3 실시간 시뮬레이터 (플랜트·고장 주입) | `UgvPlant` | `ucmssim/plant.py` |
| 5.3 VCU 기존 제어 SW·BIT | `VcuControlSoftware`, `DTC_TABLE` | `ucmssim/vcu/control.py` |
| 6.3 제어 SW ↔ 어댑터 인터페이스 | `DiagnosticChannel` (링버퍼 + 공유 메모리) | `ucmssim/vcu/channel.py` |
| 6.3 Fault Mapper / Characteristic Sampler / Health Evaluator / Usage Counter / PBIT Reporter / DDS Publisher | 동명 클래스, `UcmAdapter` | `ucmssim/vcu/adapter.py` |
| VCU 부팅·PBIT·재기동 | `VcuNode`, `NvmStore` | `ucmssim/vcu/node.py`, `nvm.py` |
| 단순 장비 (RQ3) | `SimpleTempSensor` | `ucmssim/simple_device.py` |
| 6.4 Spec Registry (브리지 포함) | `SpecRegistry` | `ucmssim/ucms/spec_registry.py` |
| 6.4 Health Republisher | `HealthRepublisher` | `ucmssim/ucms/health_republisher.py` |
| 6.4 Fault Event Deriver | `FaultEventDeriver` | `ucmssim/ucms/fault_events.py` |
| 6.4 Threshold Evaluator (6.5 상태도) | `LimitMonitor`, `ThresholdEvaluator` | `ucmssim/ucms/threshold.py` |
| 6.4 Usage Tracker | `UsageTracker` | `ucmssim/ucms/usage_tracker.py` |
| 6.4 Persistence (SQLite) | `UcmsStore` | `ucmssim/ucms/persistence.py` |
| 6.2 DDS I/O | `UcmsService` | `ucmssim/ucms/service.py` |
| 간이 HMI | `SimpleHmi` (텍스트 화면·상태 사전) | `ucmssim/hmi.py` |
| 간이 HMI 화면 (6.8) | `SimRuntime`(실행·명령), `HmiApp`(tkinter), `TrendChart` | `ucmssim/gui/` |
| 6.7 DDS·QoS | 모의 DDS (`Domain`, `Qos`) | `ucmssim/dds.py`, `config/qos.yaml` |
| A.4.5 UCM 토픽 | 토픽 데이터클래스 | `ucmssim/topics.py` |
| 6.9 설정 파일 | `vcu_ucm.yaml`, `ucms_bridge.yaml` | `config/` |
| 8.2 고장 주입 시나리오, 5.4 지연 측정 | `SCENARIOS`, `Testbed`, `HarnessObserver` | `ucmssim/harness/` |
| 2.2 성공 기준 판정 | `evaluate()` | `ucmssim/harness/report.py` |
| 편차 기록부 | `DEVIATIONS` | `ucmssim/deviation.py` |

### 주요 동작

- **고장코드 상태 전이** (6.5절): `auto` 고장은 조건 해소 시 Inactive, `latch` 고장은 Latched로 남고
  HMI의 운용자 해제(`Fault_Code__clearFault`) 또는 VCU 재기동으로 해제, `restart` 고장(F06)은 재기동 시 해제.
- **건전성** (7.4절): PBIT 실패 또는 심각도 high 고장(래치 포함) 활성 → RED. UCMS는 리소스 판정값을 그대로 재발행.
  임계값 초과는 건전성에 영향을 주지 않는다.
- **임계값 판정**: attack/release 연속 횟수 + 값 불감대(hysteresis). 판정 결과는 `Threshold_Exceedance_Event`.
- **저장 후 발행**: 이벤트 상태 전이는 SQLite `event_log` 에 먼저 기록한 뒤 발행한다 (SC5).
- **재기동 복원** (SC6): UCMS는 기동 시 DB의 활성 이벤트를 복원·재발행하고, TRANSIENT_LOCAL로 재생되는
  `Fault_Code` 와 대조해 중복 없이 정합을 맞춘다. 정지 중 해소된 고장은 재기동 직후 해제된다.
- **브리지** (6.4절): UCMS 기동 후 5 s 동안 리소스 사양을 기다리고, 없으면 `ucms_bridge.yaml` 사양을 대리 발행한다.
  늦게라도 리소스가 사양을 발행하면 리소스 사양으로 교체한다. 채택 근거는 DB `spec_adoption` 에 남는다.
- **최소 침습** (1장): 기존 제어 SW의 UCM 연동부는 `# [UCM 연동]` 표시된 채널 쓰기 호출뿐이다.
  어댑터 예외는 제어로 전파되지 않으며, FI-13에서 UCM 연동 유무·어댑터 정지와 무관하게 제어 출력이 같음을 확인한다.

## 시나리오 (설계안 8.2절)

| ID | 주입 | t0 | 통과 조건 (SIL) | SC |
|---|---|---|---|---|
| FI-01 | 인버터 CAN 차단 (양방향) | 차단 시각 | F01 Fault_Event, RED, DB 기록, 복구 시 같은 이벤트 해제·GREEN | SC2, SC3 |
| FI-02 | 모터 온도 램프 (4 °C/s → 160 °C → 80 °C) | 150 °C 통과 시각 | C01 임계 초과, F02 활성→래치(RED 유지), 운용자 해제 → GREEN | SC2, SC3 |
| FI-03 | 배터리 전압 강하 (−10 V/s → 280 V → 복귀) | 300 V 통과 시각 | C02 하한 초과, F03 활성→자동 해제, GREEN | SC2, SC3 |
| FI-04 | 모터 온도센서 단선 | 주입 시각 | F04(medium) 활성, GREEN 유지, 단선 중 C01 무효값 미발행 | SC2 |
| FI-05 | 좌측 휠속도 센서 고착 + 가속 | 주입 시각 | F05 활성·해제 | SC2 |
| FI-06 | NVM CRC 손상 후 재기동 | 재기동 시각 | F06(PBIT) + PBIT 실패·RED, 다음 재기동에 해제 | SC2 |
| FI-07 | 140 °C ± 3 °C, 3 s 주기 진동 60 s | — | 임계 초과 이벤트 ≤ 1건 (단순 비교 시 건수도 보고) | SC4 |
| FI-08 | F01 활성 중 UCMS 정지 3 s 후 재기동 | 재기동 시각 | 2 s 내 고장·RED 복원, 같은 이벤트 ID, DB 중복 없음 | SC6 |
| FI-09 | F01 활성 5 s 후 HMI 접속 | 접속 시각 | 0.5 s 내 RED·활성 고장 표시 | SC6 |
| FI-10 | 단순 장비 실내온도 램프 → 60 °C | 50 °C 통과 시각 | 브리지 사양 채택, 브리지 사양으로 임계 이벤트 생성 | SC2, SC7 |
| FI-11 | 정상 운전 (순항·감속·정차 반복) 1시간 | — | 고장·임계 이벤트 0건, RED 없음 | SC4 |
| FI-12 | 타입 정보 없이 원시 해석하는 이종 구독자 | — | 전 토픽 수신·스키마 일치·QoS 호환 (**모의 수준**) | SC9 |
| FI-13 | 0↔60 km/h 가감속 + 비상정지, 3변형(A 연동 없음/B 연동/C 어댑터 정지) | — | 제어 출력 해시 A=B=C. 실행시간은 참고값 | SC8 |

## 결과 예시 (`python -m ucmssim run`, seed 1)

| ID | 결과 | 판정 |
|---|---|---|
| SC1 | 선정 6항목 100% 매핑, 제외 2항목 사유 기록 | 충족 |
| SC2 | 7/7종 탐지 (실행 94/94) | 충족 |
| SC3 | p95 = 0.669 s (n=90, 최대 0.686 s) — FI-01 p95 0.208 s, FI-02 0.683 s, FI-03 0.625 s | 충족 |
| SC4 | FI-07 임계 초과 0건 (단순 비교 시 20건), FI-11 1시간 오경보 0건 | 충족 |
| SC5 | 100회 실행 합계 DB 누락 0건 | 충족 |
| SC6 | FI-08 복원 0.01 s, FI-09 표시 0.003 s | 충족 |
| SC7 | 브리지 사양으로 임계 이벤트 생성 | 충족 |
| SC8 | 제어 출력 동일성 확인, 실행시간 차이는 측정 잡음 수준 | 보류 (기준값 TBD, 타깃 지터는 T4) |
| SC9 | 12/12 토픽 수신 | 충족 (모의 수준) |

지연 구성: BIT 검출(CAN 타임아웃 0.1 s, 과온·저전압 디바운스 0.5 s) + 어댑터 주기(≤ 0.1 s) + DDS 전송 + UCMS 처리 5 ms.
SC3 여유는 대부분 BIT 디바운스에 달려 있으므로 ICD 확정 시 디바운스 값과 함께 재평가해야 한다
(`tests/test_scenarios.py` 의 음성 시험: 디바운스 2 s → SC3 미충족 판정).

## SIL의 한계와 HILS 이행 시 교체 지점

| 항목 | SIL 구현 | T3·T4 (HILS) |
|---|---|---|
| 미들웨어 | `dds.py` 모의 DDS (deadline·liveliness 미모사) | Cyclone DDS / Fast DDS / OpenDDS (D4). `dds.py` 인터페이스만 교체 |
| 토픽 타입 | `topics.py` 자체 정의 (DEV-001) | LDM/NGVA IDL 생성 코드 (D1, R2) |
| VCU | Python 제어·BIT 모사 | 실 VCU 타깃 + C/C++ 어댑터 (방식 A/B, D3) |
| 플랜트·고장 주입 | `plant.py` 1차 모델, 오버라이드 | 실시간 시뮬레이터 플랜트 모델·고장 주입 블록 |
| 시간 | 단일 모의 시계 (DEV-010) | 하니스 PC 시계로 t0·t1 기록 (5.4절) |
| SC8 지터 | 제어 출력 동일성만 확인 | 타깃에서 제어 주기 지터 측정 |
| SC9 | 원시 해석 구독자 (모의) | 이종 DDS 구현 간 실제 송수신 |

편차 전체 목록은 `python -m ucmssim run` 실행 후 `out/deviation_log.md` 또는 `ucmssim/deviation.py` 참고.

## 디렉터리

```
ucms_sim/
├── config/                 vcu_ucm.yaml, ucms_bridge.yaml, qos.yaml, sim.yaml
├── ucmssim/
│   ├── sched.py            모의 시계·사건 스케줄러
│   ├── dds.py              모의 DDS
│   ├── topics.py           UCM 토픽 타입
│   ├── config.py           설정 로더 (TBD 거부)
│   ├── plant.py            플랜트 모델·고장 주입
│   ├── vcu/                기존 제어 SW·BIT, 진단 채널, UCM 어댑터, 노드 수명주기, NVM
│   ├── simple_device.py    단순 장비
│   ├── ucms/               최소 UCMS
│   ├── hmi.py              간이 HMI
│   ├── gui/                간이 HMI 화면 (runtime·theme·widgets·app, tkinter)
│   ├── deviation.py        편차 기록부
│   ├── demo.py             서술형 데모
│   └── harness/            시험대·시나리오·판정·보고서
└── tests/                  T1 단위시험, T2 SIL 통합시험
```
