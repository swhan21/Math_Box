"""T1 단위시험: 임계값 판정기 (6.5절 상태도)."""

import unittest

from ucmssim.topics import ExceedanceState, LimitType
from ucmssim.ucms.threshold import LimitMonitor, MonitorState

EXC, REL = ExceedanceState.EXCEEDED, ExceedanceState.RELEASED


def feed(mon, values):
    return [mon.update(v) for v in values]


class LimitMonitorTest(unittest.TestCase):
    def test_attack_requires_consecutive_samples(self):
        mon = LimitMonitor(LimitType.MAX, 100.0, hysteresis=0.0, attack=3, release=3)
        self.assertEqual(feed(mon, [101, 102, 99]), [None, None, None])     # Attack → Normal 복귀
        self.assertIs(mon.state, MonitorState.NORMAL)
        self.assertEqual(feed(mon, [101, 102, 103]), [None, None, EXC])
        self.assertIs(mon.state, MonitorState.EXCEEDED)

    def test_release_requires_consecutive_samples_and_reexceed_resets(self):
        mon = LimitMonitor(LimitType.MAX, 100.0, attack=1, release=3)
        self.assertEqual(mon.update(105), EXC)
        self.assertEqual(feed(mon, [99, 99, 101]), [None, None, None])       # Release → Exceeded (재초과)
        self.assertIs(mon.state, MonitorState.EXCEEDED)
        self.assertEqual(feed(mon, [99, 99, 99]), [None, None, REL])
        self.assertIs(mon.state, MonitorState.NORMAL)

    def test_hysteresis_band_blocks_release(self):
        mon = LimitMonitor(LimitType.MAX, 100.0, hysteresis=2.0, attack=1, release=1)
        mon.update(101)
        self.assertIsNone(mon.update(99.0))                                  # 불감대(98~100) 안 → 해제 아님
        self.assertEqual(mon.update(98.0), REL)

    def test_min_limit(self):
        mon = LimitMonitor(LimitType.MIN, 310.0, hysteresis=2.0, attack=2, release=2)
        self.assertEqual(feed(mon, [309, 308]), [None, EXC])
        self.assertEqual(feed(mon, [311, 312, 313]), [None, None, REL])     # 311은 불감대 안

    def test_oscillation_near_limit_does_not_chatter(self):
        """FI-07 축소판: 3샘플 주기 진동은 attack=3 을 넘지 못한다. 단순 비교는 매 주기 이벤트."""
        values = [100 + d for d in (0.0, 2.6, -2.6) * 20]
        hyst = LimitMonitor(LimitType.MAX, 100.0, hysteresis=2.0, attack=3, release=3)
        naive = LimitMonitor(LimitType.MAX, 100.0, attack=1, release=1)
        self.assertEqual(feed(hyst, values).count(EXC), 0)
        self.assertEqual(feed(naive, values).count(EXC), 20)

    def test_force_exceeded_for_restore(self):
        mon = LimitMonitor(LimitType.MAX, 100.0, attack=3, release=1)
        mon.force_exceeded()
        self.assertEqual(mon.update(90), REL)

    def test_invalid_counts(self):
        with self.assertRaises(ValueError):
            LimitMonitor(LimitType.MAX, 1.0, attack=0)


if __name__ == "__main__":
    unittest.main()
