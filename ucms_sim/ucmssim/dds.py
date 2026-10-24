"""프로세스 내 모의 DDS (편차 DEV-002).

실제 DDS(Cyclone DDS / Fast DDS / OpenDDS) 대신 SIL 단계에서 쓰는 최소 모사다.
API는 DDS 개념(DomainParticipant / DataWriter / DataReader / QoS)을 따르므로
실제 DDS 바인딩으로 바꿀 때 이 모듈만 교체하면 된다.

모사하는 것
  - 토픽 단위 발행/구독, 키 인스턴스, dispose
  - DURABILITY (VOLATILE / TRANSIENT_LOCAL: 늦은 접속자에게 인스턴스별 최신값 전달)
  - RELIABILITY (RELIABLE / BEST_EFFORT: 손실률 적용), HISTORY KEEP_LAST depth
  - LIFESPAN (수명이 지난 샘플은 전달하지 않음)
  - QoS 호환성 검사 (RxO: offered >= requested, 불일치 시 매칭 안 됨)
  - 네트워크 지연 (발행자-구독자 쌍별 순서 보장), 직렬화 (노드 간 객체 공유 금지)
모사하지 않는 것: DEADLINE, LIVELINESS, 파티션, 보안, 실제 RTPS 패킷
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from . import topics as T
from .sched import Scheduler


class Durability(str, Enum):
    VOLATILE = "VOLATILE"
    TRANSIENT_LOCAL = "TRANSIENT_LOCAL"


class Reliability(str, Enum):
    BEST_EFFORT = "BEST_EFFORT"
    RELIABLE = "RELIABLE"


_DURABILITY_RANK = {Durability.VOLATILE: 0, Durability.TRANSIENT_LOCAL: 1}
_RELIABILITY_RANK = {Reliability.BEST_EFFORT: 0, Reliability.RELIABLE: 1}


@dataclass(frozen=True)
class Qos:
    durability: Durability = Durability.VOLATILE
    reliability: Reliability = Reliability.RELIABLE
    depth: int = 1
    lifespan_s: float | None = None

    def incompatibilities(self, requested: Qos) -> list[str]:
        """self = 발행자가 제공하는(offered) QoS. DDS RxO 규칙: offered >= requested."""
        reasons = []
        if _RELIABILITY_RANK[self.reliability] < _RELIABILITY_RANK[requested.reliability]:
            reasons.append(
                f"RELIABILITY offered={self.reliability.value} < requested={requested.reliability.value}"
            )
        if _DURABILITY_RANK[self.durability] < _DURABILITY_RANK[requested.durability]:
            reasons.append(
                f"DURABILITY offered={self.durability.value} < requested={requested.durability.value}"
            )
        return reasons


@dataclass(frozen=True)
class LatencyModel:
    min_s: float = 0.0005
    max_s: float = 0.003
    best_effort_loss: float = 0.0


@dataclass(frozen=True)
class SampleInfo:
    alive: bool                 # False = dispose 통지 (sample 은 None)
    key: tuple
    source_timestamp: float
    reception_timestamp: float
    publisher: str              # 발행 participant 이름


@dataclass
class DomainStats:
    sent: int = 0
    delivered: int = 0
    lost: int = 0
    expired: int = 0


DataCallback = Callable[[Any, SampleInfo], None]


class Domain:
    """DDS 도메인 = 모의 네트워크. 매칭·전송·지연을 담당한다."""

    def __init__(
        self,
        scheduler: Scheduler,
        domain_id: int = 0,
        latency: LatencyModel | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.sched = scheduler
        self.domain_id = domain_id
        self.latency = latency or LatencyModel()
        self.rng = rng or random.Random(0)
        self._writers: dict[str, list[DataWriter]] = {}
        self._readers: dict[str, list[DataReader]] = {}
        self._pair_last: dict[tuple[DataWriter, DataReader], float] = {}
        self.incompatible: list[tuple[str, str, str, list[str]]] = []
        self.stats = DomainStats()

    def participant(self, name: str, vendor: str = "mock-A") -> Participant:
        return Participant(self, name, vendor)

    # -------------------------------------------------------------- 매칭

    def _add_writer(self, w: DataWriter) -> None:
        self._writers.setdefault(w.topic, []).append(w)
        for r in self._readers.get(w.topic, []):
            self._match(w, r, late_joiner=False)

    def _add_reader(self, r: DataReader) -> None:
        self._readers.setdefault(r.topic, []).append(r)
        for w in self._writers.get(r.topic, []):
            self._match(w, r, late_joiner=True)

    def _match(self, w: DataWriter, r: DataReader, late_joiner: bool) -> None:
        if r.ignore_local and r.participant is w.participant:
            return
        reasons = w.qos.incompatibilities(r.qos)
        if reasons:
            self.incompatible.append((w.topic, w.participant.name, r.participant.name, reasons))
            return
        w._matched.append(r)
        if (
            late_joiner
            and w.qos.durability is Durability.TRANSIENT_LOCAL
            and r.qos.durability is Durability.TRANSIENT_LOCAL
        ):
            for key, history in w._history.items():
                for payload, src_ts in history:
                    self._transmit(w, r, True, key, payload, src_ts)

    def _remove_writer(self, w: DataWriter) -> None:
        self._writers[w.topic].remove(w)

    def _remove_reader(self, r: DataReader) -> None:
        self._readers[r.topic].remove(r)
        for w in self._writers.get(r.topic, []):
            if r in w._matched:
                w._matched.remove(r)

    # -------------------------------------------------------------- 전송

    def _transmit(
        self, w: DataWriter, r: DataReader, alive: bool, key: tuple, payload: bytes, src_ts: float
    ) -> None:
        self.stats.sent += 1
        reliable = w.qos.reliability is Reliability.RELIABLE and r.qos.reliability is Reliability.RELIABLE
        if not reliable and self.latency.best_effort_loss and self.rng.random() < self.latency.best_effort_loss:
            self.stats.lost += 1
            return
        t = self.sched.now + self.rng.uniform(self.latency.min_s, self.latency.max_s)
        pair = (w, r)
        t = max(t, self._pair_last.get(pair, 0.0))      # 발행자-구독자 쌍별 순서 보장
        self._pair_last[pair] = t
        self.sched.call_at(t, r._deliver, alive, key, payload, src_ts, w.participant.name, w.qos.lifespan_s)


class Participant:
    def __init__(self, domain: Domain, name: str, vendor: str) -> None:
        self.domain = domain
        self.name = name
        self.vendor = vendor
        self.closed = False
        self._entities: list[DataWriter | DataReader] = []

    def create_writer(self, topic: str, qos: Qos) -> DataWriter:
        self._check_open(topic)
        w = DataWriter(self, topic, qos)
        self._entities.append(w)
        self.domain._add_writer(w)
        return w

    def create_reader(
        self,
        topic: str,
        qos: Qos,
        on_data: DataCallback | None = None,
        *,
        raw: bool = False,
        ignore_local: bool = False,
    ) -> DataReader:
        """raw=True 이면 타입 정보 없이 dict 로 해석한다 (이종 구현 구독자 모사)."""
        self._check_open(topic)
        r = DataReader(self, topic, qos, on_data, raw, ignore_local)
        self._entities.append(r)
        self.domain._add_reader(r)
        return r

    def close(self) -> None:
        """참여자 종료 (프로세스 종료·재기동 모사). 발행 이력도 함께 사라진다."""
        for e in self._entities:
            e._close()
        self._entities.clear()
        self.closed = True

    def _check_open(self, topic: str) -> None:
        if self.closed:
            raise RuntimeError(f"participant {self.name} is closed")
        if topic not in T.TOPICS:
            raise KeyError(f"unknown topic: {topic}")


class DataWriter:
    def __init__(self, participant: Participant, topic: str, qos: Qos) -> None:
        self.participant = participant
        self.topic = topic
        self.qos = qos
        self.type = T.TOPICS[topic]
        self.write_count = 0
        self.closed = False
        self._history: dict[tuple, deque] = {}
        self._matched: list[DataReader] = []

    def write(self, sample: Any) -> None:
        if self.closed:
            raise RuntimeError(f"writer {self.topic} is closed")
        if type(sample) is not self.type:
            raise TypeError(f"{self.topic} expects {self.type.__name__}, got {type(sample).__name__}")
        domain = self.participant.domain
        key = T.key_of(sample)
        payload = T.encode(sample)
        src_ts = domain.sched.now
        self.write_count += 1
        if self.qos.durability is Durability.TRANSIENT_LOCAL:
            self._history.setdefault(key, deque(maxlen=self.qos.depth)).append((payload, src_ts))
        for r in list(self._matched):
            domain._transmit(self, r, True, key, payload, src_ts)

    def dispose(self, key: tuple) -> None:
        """인스턴스 삭제. 늦은 접속자에게 더 이상 전달되지 않는다."""
        domain = self.participant.domain
        self._history.pop(key, None)
        for r in list(self._matched):
            domain._transmit(self, r, False, key, b"", domain.sched.now)

    def _close(self) -> None:
        if not self.closed:
            self.closed = True
            self._history.clear()
            self.participant.domain._remove_writer(self)


class DataReader:
    def __init__(
        self,
        participant: Participant,
        topic: str,
        qos: Qos,
        on_data: DataCallback | None,
        raw: bool,
        ignore_local: bool,
    ) -> None:
        self.participant = participant
        self.topic = topic
        self.qos = qos
        self.type = None if raw else T.TOPICS[topic]
        self.on_data = on_data
        self.ignore_local = ignore_local
        self.closed = False
        self.received = 0
        self.instances: dict[tuple, Any] = {}       # 인스턴스별 최신 샘플 (KEEP_LAST 1)

    def _deliver(
        self, alive: bool, key: tuple, payload: bytes, src_ts: float, publisher: str, lifespan: float | None
    ) -> None:
        if self.closed:
            return
        domain = self.participant.domain
        now = domain.sched.now
        if lifespan is not None and now - src_ts > lifespan:
            domain.stats.expired += 1
            return
        domain.stats.delivered += 1
        self.received += 1
        if alive:
            sample = T.decode_raw(payload) if self.type is None else T.decode(self.type, payload)
            self.instances[key] = sample
        else:
            sample = None
            self.instances.pop(key, None)
        if self.on_data is not None:
            self.on_data(sample, SampleInfo(alive, key, src_ts, now, publisher))

    def _close(self) -> None:
        if not self.closed:
            self.closed = True
            self.participant.domain._remove_reader(self)
