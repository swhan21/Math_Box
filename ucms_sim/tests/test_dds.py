"""T1 단위시험: 모의 DDS의 QoS 동작."""

import unittest

from ucmssim.dds import Domain, Durability, LatencyModel, Qos, Reliability
from ucmssim.sched import Scheduler
from ucmssim.topics import CharacteristicValue, FaultClearCommand, Health, ResourceWithMonitoring

STATE = Qos(Durability.TRANSIENT_LOCAL, Reliability.RELIABLE, depth=1)
VOLATILE = Qos(Durability.VOLATILE, Reliability.RELIABLE, depth=1)


def rwm(rid="R1", health=Health.GREEN, t=0.0):
    return ResourceWithMonitoring(rid, health, True, True, 0, t)


class DdsTest(unittest.TestCase):
    def setUp(self):
        self.sched = Scheduler()
        self.domain = Domain(self.sched, latency=LatencyModel(0.001, 0.002))
        self.pub = self.domain.participant("PUB")
        self.sub = self.domain.participant("SUB")

    def collect(self, topic, qos, **kw):
        got = []
        self.sub.create_reader(topic, qos, lambda s, i: got.append((s, i)), **kw)
        return got

    def test_transient_local_late_joiner_gets_latest_per_instance(self):
        w = self.pub.create_writer(ResourceWithMonitoring.TOPIC, STATE)
        w.write(rwm("R1", Health.GREEN))
        w.write(rwm("R1", Health.RED))
        w.write(rwm("R2", Health.GREEN))
        self.sched.run_for(1.0)
        got = self.collect(ResourceWithMonitoring.TOPIC, STATE)
        self.sched.run_for(0.1)
        self.assertEqual(sorted((s.resource_id, s.health) for s, _ in got),
                         [("R1", Health.RED), ("R2", Health.GREEN)])

    def test_volatile_late_joiner_gets_nothing(self):
        w = self.pub.create_writer(ResourceWithMonitoring.TOPIC, VOLATILE)
        w.write(rwm())
        self.sched.run_for(1.0)
        got = self.collect(ResourceWithMonitoring.TOPIC, VOLATILE)
        self.sched.run_for(0.1)
        self.assertEqual(got, [])

    def test_incompatible_qos_is_not_matched(self):
        self.pub.create_writer(ResourceWithMonitoring.TOPIC, VOLATILE)
        self.collect(ResourceWithMonitoring.TOPIC, STATE)                  # TRANSIENT_LOCAL 요청
        self.assertEqual(len(self.domain.incompatible), 1)
        self.assertIn("DURABILITY", self.domain.incompatible[0][3][0])

    def test_lifespan_expires_old_samples(self):
        qos = Qos(Durability.TRANSIENT_LOCAL, Reliability.RELIABLE, depth=1, lifespan_s=10.0)
        w = self.pub.create_writer(FaultClearCommand.TOPIC, qos)
        w.write(FaultClearCommand("R1", "F02", "crew", 0.0))
        self.sched.run_for(11.0)
        got = self.collect(FaultClearCommand.TOPIC, qos)
        self.sched.run_for(0.1)
        self.assertEqual(got, [])
        self.assertEqual(self.domain.stats.expired, 1)

    def test_dispose_removes_instance_for_late_joiners(self):
        w = self.pub.create_writer(ResourceWithMonitoring.TOPIC, STATE)
        early = self.collect(ResourceWithMonitoring.TOPIC, STATE)
        w.write(rwm())
        w.dispose(("R1",))
        self.sched.run_for(0.1)
        self.assertEqual([i.alive for _, i in early], [True, False])
        late = self.collect(ResourceWithMonitoring.TOPIC, STATE)
        self.sched.run_for(0.1)
        self.assertEqual(late, [])

    def test_order_preserved_per_writer_reader_pair(self):
        w = self.pub.create_writer(CharacteristicValue.TOPIC, VOLATILE)
        got = self.collect(CharacteristicValue.TOPIC, VOLATILE)
        for k in range(50):
            w.write(CharacteristicValue("R1", "C01", float(k), 0.0))
        self.sched.run_for(0.1)
        self.assertEqual([s.value for s, _ in got], [float(k) for k in range(50)])

    def test_samples_are_copies_and_ignore_local(self):
        w = self.pub.create_writer(ResourceWithMonitoring.TOPIC, STATE)
        own = []
        self.pub.create_reader(ResourceWithMonitoring.TOPIC, STATE, lambda s, i: own.append(s), ignore_local=True)
        got = self.collect(ResourceWithMonitoring.TOPIC, STATE)
        sample = rwm()
        w.write(sample)
        self.sched.run_for(0.1)
        self.assertEqual(own, [])
        self.assertEqual(got[0][0], sample)
        self.assertIsNot(got[0][0], sample)

    def test_raw_reader_and_type_check(self):
        w = self.pub.create_writer(ResourceWithMonitoring.TOPIC, STATE)
        got = self.collect(ResourceWithMonitoring.TOPIC, STATE, raw=True)
        w.write(rwm(health=Health.RED))
        self.sched.run_for(0.1)
        self.assertEqual(got[0][0]["health"], "RED")
        with self.assertRaises(TypeError):
            w.write(CharacteristicValue("R1", "C01", 1.0, 0.0))

    def test_closed_participant_stops_delivery(self):
        w = self.pub.create_writer(ResourceWithMonitoring.TOPIC, STATE)
        got = self.collect(ResourceWithMonitoring.TOPIC, STATE)
        self.sub.close()
        w.write(rwm())
        self.sched.run_for(0.1)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
