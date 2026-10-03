"""Quiet admission survives phase changes, never activity or evidence gaps."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host_monitor import HostMonitor


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.clock=patch('host_monitor.time.monotonic',return_value=1000)
        self.clock.start();self.addCleanup(self.clock.stop)
        self.source=self.root/'quiet.json'
        self.value=dict(version=1,boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            last_active=600,last_sample=998,samples=80,previous={})
        self.source.write_text(json.dumps(self.value))

    def monitor(self,name='phase',**kwargs):
        folder=self.root/name;folder.mkdir()
        return HostMonitor(folder,continuity=self.source,**kwargs)

    def sample_once(self,monitor,active=None,stamp=1000):
        sample=dict(at_ms=1000000,monotonic=stamp,builds={},active_builds=active or [])
        with patch('host_monitor.observe',return_value=sample),patch.object(monitor.stop,'wait',side_effect=lambda _:monitor.stop.set()):
            monitor.run()

    def test_phase_inherits_quiet_and_records_evidence_after_own_sample(self):
        monitor=self.monitor();monitor.inherit_quiet()
        self.assertEqual(monitor.last_active,600);self.assertEqual(monitor.samples,0)
        self.sample_once(monitor)
        receipt=monitor.wait_quiet()
        self.assertEqual(receipt['quiet_seconds'],400)
        self.assertEqual(receipt['sample_count'],1)
        evidence=json.loads((monitor.directory/'quiet-continuity.json').read_text())
        self.assertTrue(evidence['accepted'])
        self.assertEqual(evidence['sha256'],receipt['continuity']['sha256'])
        self.assertEqual(evidence['checkpoint'],self.value)

    def test_new_activity_resets_inherited_credit_and_is_an_overlap(self):
        monitor=self.monitor();monitor.inherit_quiet();self.sample_once(monitor,['123:456'])
        self.assertEqual(monitor.last_active,1000)
        self.assertEqual(monitor.overlap(999999,1000001),[1000000])
        with patch('host_monitor.time.monotonic',side_effect=[1000,1001,1001,1001]):
            with self.assertRaises(TimeoutError):monitor.wait_quiet(max_wait=.5)

    def test_missing_stale_wrong_boot_or_invalid_checkpoint_gets_no_credit(self):
        cases=[None,dict(last_sample=979),dict(last_sample=1001),dict(last_active=999),
            dict(last_active=float('nan')),dict(boot_id='another-boot'),dict(samples=0),dict(previous=[])]
        for index,change in enumerate(cases):
            with self.subTest(change=change):
                if change is None:self.source.unlink()
                else:self.source.write_text(json.dumps(dict(self.value,**change)))
                monitor=self.monitor(str(index));monitor.inherit_quiet()
                self.assertEqual(monitor.last_active,1000)
                self.assertIsNone(monitor.inherited)
                self.assertFalse(json.loads((monitor.directory/'quiet-continuity.json').read_text())['accepted'])

    def test_parent_checkpoint_preserves_parked_build_identity(self):
        self.value['previous']={'123:456':dict(user_ticks=2,system_ticks=1)}
        self.source.write_text(json.dumps(self.value))
        monitor=self.monitor(publish_quiet=True);monitor.inherit_quiet()
        self.assertEqual(monitor.previous,self.value['previous'])
        self.sample_once(monitor)
        value=json.loads(monitor.quiet_path.read_text())
        self.assertEqual(value['last_active'],600)
        self.assertEqual(value['last_sample'],1000)
        self.assertEqual(value['boot_id'],self.value['boot_id'])

    def test_resumed_sampling_does_not_certify_an_unobserved_gap(self):
        monitor=self.monitor(publish_quiet=True);monitor.inherit_quiet()
        monitor.last_sample=970
        self.sample_once(monitor)
        self.assertEqual(monitor.last_active,1000)
        self.assertEqual(json.loads(monitor.quiet_path.read_text())['last_active'],1000)


if __name__=='__main__':unittest.main()
