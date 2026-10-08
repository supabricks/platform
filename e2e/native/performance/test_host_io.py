"""Host attribution must retain gaps and never mistake PID reuse for I/O."""
import copy
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host_io import HostIO, deltas, observe
from compare import run_trial


class HostIOTests(unittest.TestCase):
    def sample(self):
        return dict(at_ms=1000,processes=[dict(pid=123,start_ticks=10,ppid=1,role='other',
            cgroup_sha256='abc',io=dict(read_bytes=10,write_bytes=20),user_ticks=5,system_ticks=3)])

    def test_pid_reuse_is_gap_not_throughput(self):
        a=self.sample();b=copy.deepcopy(a);b['at_ms']=2000;b['processes'][0]['start_ticks']=11
        r=deltas(a,b);self.assertEqual(r['processes'],[])
        self.assertEqual((r['new_identities'],r['exited_identities']),(1,1))

    def test_reset_and_access_denial_are_unavailable(self):
        a=self.sample();b=copy.deepcopy(a);b['at_ms']=2000;b['processes'][0]['io']['write_bytes']=0
        self.assertEqual(deltas(a,b)['unavailable'][0]['reason'],'counter_reset_or_group_changed')
        b['processes'][0]['io']=None
        self.assertEqual(deltas(a,b)['unavailable'][0]['reason'],'permission_denied')

    def test_stable_identity_delta(self):
        a=self.sample();b=copy.deepcopy(a);b['at_ms']=2000;b['processes'][0]['io']['write_bytes']=120
        self.assertEqual(deltas(a,b)['processes'][0]['write_bytes'],100)

    def test_process_text_and_cgroup_paths_are_not_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=root/'123';p.mkdir()
            # stat fields after comm: state,ppid,...utime,stime,...starttime
            fields=['0']*20;fields[0]='S';fields[1]='1';fields[11]='5';fields[12]='3';fields[19]='10'
            (p/'stat').write_text('123 (private name) '+' '.join(fields))
            (p/'comm').write_text('private name');(p/'cgroup').write_text('0::/private/user/workload')
            (p/'io').write_text('read_bytes: 10\nwrite_bytes: 20\n')
            result=observe(root);self.assertEqual(result['processes'][0]['role'],'other')
            self.assertNotIn('private',json.dumps(result))

    def test_budget_failure_preserves_partial_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor=HostIO(Path(tmp)/'report.json.gz',interval=.001,max_bytes=1)
            with patch('host_io.observe',return_value=self.sample()):
                monitor.start();monitor.thread.join(timeout=2)
            with self.assertRaisesRegex(RuntimeError,'budget'):monitor.close()
            data=json.loads(gzip.decompress(monitor.path.read_bytes()))
            self.assertIn('budget',data['error']);self.assertEqual(data['samples'],[])

    def test_read_failure_is_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor=HostIO(Path(tmp)/'report.json.gz')
            with patch('host_io.observe',side_effect=OSError('probe failed')):
                monitor.start();monitor.thread.join(timeout=2)
            with self.assertRaisesRegex(RuntimeError,'probe failed'):monitor.close()

    def test_arm_selection_and_evidence_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/'trial'
            def measured(*args):
                directory.mkdir(exist_ok=True)
                return dict(evidence_sha256={})
            with patch('compare.run_measured_trial',side_effect=measured),patch('host_io.observe',return_value=self.sample()):
                off=run_trial(dict(host_io='candidate'),'predecessor',{},directory,None,{})
                self.assertIn('observer-controller.json',off['evidence_sha256'])
                self.assertFalse((directory/'host-io.json.gz').exists())
                on=run_trial(dict(host_io='candidate'),'candidate',{},directory,None,{})
                self.assertIn('host-io.json.gz',on['evidence_sha256'])

    def test_disk_floor_rejects_before_child_launch(self):
        with tempfile.TemporaryDirectory() as tmp,patch('compare.run_measured_trial') as measured:
            with self.assertRaisesRegex(ValueError,'disk'):
                run_trial(dict(minimum_free_gib=10**12),'candidate',{},Path(tmp)/'trial',None,{})
            measured.assert_not_called()
