"""Regression checks for warm-request attribution in the SP09b evidence screen."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from parallelism_screen import screen


class ScreenTests(unittest.TestCase):
    def fixture(self, root):
        warm=dict(started_at_ms=150,final=True,worker_request_index=2,
                  counter_scope='process_cumulative',request_cpu_s=.2,
                  cpu_user_s=1000,cpu_system_s=500,pid=42,
                  metrics={'apply.run':dict(calls=1,errors=0,total_ns=400_000_000),
                           'durability.fsync':dict(total_ns=150_000_000),
                           'apply.apply_table':dict(calls=2,total_ns=100_000_000)})
        cold=copy.deepcopy(warm);cold['worker_request_index']=1
        failed=copy.deepcopy(warm);failed['metrics']['apply.run']['errors']=1
        incomplete=copy.deepcopy(warm);incomplete['final']=False
        outside=copy.deepcopy(warm);outside['started_at_ms']=99
        second=copy.deepcopy(warm);second.update(worker_request_index=3,request_cpu_s=.1,cpu_user_s=2000)
        profile={'workers':{f'incremental-{i}.jsonl':[r] for i,r in enumerate([warm,cold,failed,incomplete,outside,second])}}
        for phase in ('historical-main','qualified-main'):
            folder=root/phase/'candidate';trial=folder/'trial';trial.mkdir(parents=True)
            (trial/'profile.json.gz').write_bytes(gzip.compress(json.dumps(profile).encode()))
            (trial/'trial.json').write_text(json.dumps(dict(status='measured',measurement_start_ms=100,measurement_end_ms=200)))
            receipt=dict(directory='candidate',evidence_sha256={str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest() for p in trial.iterdir()},
                         metrics=dict(cpu_cores=1,source_rows_s=1250))
            record=dict(state='complete',pairs=[dict(pair=dict(cpus=8,rate=1250,repeat=1),results=dict(candidate=receipt))])
            (root/phase/'experiment.json').write_text(json.dumps(record))

    def test_uses_request_delta_and_preserves_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            for trial in screen(root)['trials']:
                self.assertEqual(trial['metrics']['request_cpu_ms']['median'],150)
                self.assertEqual(trial['metrics']['request_cpu_per_apply_wall']['median'],.375)
                self.assertEqual(trial['metrics']['request_cpu_ms']['count'],2)
                self.assertEqual(trial['excluded'],dict(outside_window=1,cold=1,failed_or_incomplete=2))

    def test_changed_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);self.fixture(root)
            (root/'qualified-main/candidate/trial/trial.json').write_text('{}')
            with self.assertRaises(AssertionError):screen(root)


if __name__=='__main__':unittest.main()
