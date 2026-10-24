"""VCU 노드 수명주기: 전원 인가 → 부팅 → (어댑터 기동) → PBIT → 제어 주기 실행, 재기동."""

from __future__ import annotations

from ..config import QosConfig, SimConfig, VcuUcmConfig
from ..dds import Domain, Participant
from ..plant import UgvPlant
from ..sched import Scheduler, Timer
from .adapter import UcmAdapter
from .channel import DiagnosticChannel
from .control import PbitResult, VcuControlSoftware
from .nvm import NvmStore


class VcuNode:
    """
    adapter_enabled=False : UCM 어댑터가 동작하지 않음 (어댑터 장애 모사)
    ucm_calls=False       : 제어 SW에 UCM 연동 호출 자체가 없음 (기존 SW 기준선)
    """

    def __init__(
        self,
        sched: Scheduler,
        domain: Domain,
        plant: UgvPlant,
        nvm: NvmStore,
        ucm_cfg: VcuUcmConfig,
        sim_cfg: SimConfig,
        qos: QosConfig,
        *,
        adapter_enabled: bool = True,
        ucm_calls: bool = True,
        profile: bool = False,
    ) -> None:
        self.sched = sched
        self.domain = domain
        self.plant = plant
        self.nvm = nvm
        self.ucm_cfg = ucm_cfg
        self.sim_cfg = sim_cfg
        self.qos = qos
        self.adapter_enabled = adapter_enabled
        self.ucm_calls = ucm_calls
        self.profile = profile
        self.powered = False
        self.boot_count = 0
        self.speed_setpoint_kph = 0.0
        self.control: VcuControlSoftware | None = None
        self.channel: DiagnosticChannel | None = None
        self.adapter: UcmAdapter | None = None
        self.participant: Participant | None = None
        self.pbit_result: PbitResult | None = None
        self.history: list[VcuControlSoftware] = []     # 부팅별 제어 SW 인스턴스 (분석용)
        self._timers: list[Timer] = []
        self._generation = 0

    @property
    def resource_id(self) -> str:
        return self.ucm_cfg.resource_id

    def set_speed(self, kph: float) -> None:
        self.speed_setpoint_kph = kph
        if self.control is not None:
            self.control.speed_setpoint_kph = kph

    # -------------------------------------------------------------- 수명주기

    def power_on(self) -> None:
        self.powered = True
        self._generation += 1
        self._timers.append(self.sched.call_later(self.sim_cfg.vcu.boot_s, self._boot, self._generation))

    def shutdown(self, persist: bool = True) -> None:
        self._generation += 1
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self.adapter is not None:
            self.adapter.stop(persist=persist)
        if self.participant is not None:
            self.participant.close()
        self.control = self.adapter = self.participant = self.channel = None
        self.pbit_result = None
        self.powered = False

    def reboot(self, off_s: float = 0.2) -> None:
        self.shutdown()
        self.sched.call_later(off_s, self.power_on)

    def _boot(self, generation: int) -> None:
        if generation != self._generation:
            return
        sys_block, ok = self.nvm.read("system")
        self.boot_count = (sys_block or {}).get("boot_count", 0) + 1 if ok else 1
        self.nvm.write("system", {"boot_count": self.boot_count})

        self.channel = DiagnosticChannel(self.ucm_cfg.queue_size) if self.ucm_calls else None
        self.control = VcuControlSoftware(
            self.plant, self.sim_cfg.bit, self.sim_cfg.gains, self.channel, profile=self.profile
        )
        self.control.speed_setpoint_kph = self.speed_setpoint_kph
        self.history.append(self.control)
        if self.adapter_enabled and self.channel is not None:
            self.participant = self.domain.participant(self.ucm_cfg.name)
            self.adapter = UcmAdapter(
                self.ucm_cfg, self.channel, self.participant, self.qos, self.sched, self.nvm, self.boot_count
            )
            self.adapter.start()
        self._timers.append(self.sched.call_later(self.sim_cfg.vcu.pbit_s, self._after_pbit, generation))

    def _after_pbit(self, generation: int) -> None:
        if generation != self._generation:
            return
        t = self.sched.now
        self.pbit_result = self.control.run_pbit(self.nvm, t)
        self.control.start(t)
        period = self.sim_cfg.vcu.control_period_s
        self._timers.append(
            self.sched.every(period, self._control_tick, start=t + self.sim_cfg.vcu.control_phase_s)
        )

    def _control_tick(self) -> None:
        self.control.step(self.sched.now, self.sim_cfg.vcu.control_period_s)
