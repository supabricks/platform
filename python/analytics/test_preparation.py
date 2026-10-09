"""Preparation must overlap safely without changing plan, authority or replay."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch
from capture.spool import CaptureError,canonical
from incremental import preparation as p
import incremental_worker as w
import test_incremental as f
import test_maintenance as maintenance


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.config=dict(after_lsn='0/C8',identity={'decoder_version':1},
                         storage_profile='large',deadline_ms=time.time()*1000+60000)
        self.data=(f.PROFILE,[(300,f.tx(280,300,f.change(b'I',42,new=[2,None,'two'])))],300,0)

    def test_single_result_no_state_survives_context(self):
        expected=p.decode(self.config,self.data,lambda:None)
        with p.Preparation(self.config,self.data) as job:
            self.assertEqual(job.result(),expected)
            with self.assertRaisesRegex(CaptureError,'apply_preparation_consumed'):job.result()
        self.assertFalse(job.thread.is_alive())
        self.assertIsNone(job.value);self.assertIsNone(job.data);self.assertIsNone(job.config)

    def test_background_error_is_raised_and_next_request_is_clean(self):
        with patch.object(p,'changes',side_effect=CaptureError('invalid_pgoutput')):
            with p.Preparation(self.config,self.data) as job:
                with self.assertRaisesRegex(CaptureError,'invalid_pgoutput'):job.result()
        self.assertIsNone(job.error);self.assertFalse(job.thread.is_alive())
        with p.Preparation(self.config,self.data) as next_job:
            self.assertEqual(next_job.result().end,300)

    def test_expired_preparation_is_never_admitted(self):
        with patch.object(p,'decode') as decode:
            with self.assertRaisesRegex(CaptureError,'apply_deadline'):
                with p.Preparation(dict(self.config,deadline_ms=0),self.data):pass
            decode.assert_not_called()

    def test_deadline_is_checked_after_work_before_consumption(self):
        with p.Preparation(self.config,self.data) as job:
            job.thread.join()
            with patch.object(p.time,'time',return_value=self.config['deadline_ms']/1000+1):
                with self.assertRaisesRegex(CaptureError,'apply_deadline'):job.result()
        self.assertIsNone(job.value)

    def test_cancellation_joins_background_before_propagating_main_error(self):
        started=threading.Event()
        def blocked(config,data,check):
            started.set()
            while True:
                check();time.sleep(.001)
        with patch.object(p,'decode',blocked):
            with self.assertRaisesRegex(RuntimeError,'storage failed'):
                with p.Preparation(self.config,self.data) as job:
                    self.assertTrue(started.wait(2));raise RuntimeError('storage failed')
        self.assertFalse(job.thread.is_alive());self.assertIsNone(job.error)

    def test_aggregate_boundary_does_not_split_or_consume_next_transaction(self):
        data=(f.PROFILE,[(300,b'a'),(400,b'b')],400,2)
        first=[('42',b'I',2,2,[2,None,'two'])]*65535
        with patch.object(p,'changes',side_effect=[first,[None,None]]):
            decoded=p.decode(self.config,data,lambda:None)
        self.assertEqual((len(decoded.operations),decoded.end,decoded.input_bytes),(65535,300,1))


class OverlapTests(unittest.TestCase):
    storage_profile='large'
    def setUp(self):
        previous=os.umask(0o077);self.addCleanup(os.umask,previous)
        f.IncrementalTests.setUp(self)
    tearDown=f.IncrementalTests.tearDown
    config_next=f.IncrementalTests.config_next

    def test_storage_verification_overlaps_decode_and_seals_identical_plan(self):
        self.spool.append(280,300,f.tx(280,300,
            f.change(b'U',42,new=[1,'0.12345678',f.UNCHANGED]),
            f.change(b'I',43,new=[2,'987654321.12345678','日本語'])))
        config=self.config_next('0/12C');root=Path(config['generation'])
        expected=w.plan(config,root,self.first,w.journal(config))
        started=threading.Event();verified=threading.Event();decode=p.decode;verify=w.verify_previous
        def prepare(*args):
            started.set()
            if not verified.wait(5):raise AssertionError('verification did not overlap')
            return decode(*args)
        def verification(*args):
            self.assertTrue(started.wait(5))
            try:return verify(*args)
            finally:verified.set()
        with patch.object(p,'decode',prepare),patch.object(w,'verify_previous',verification):w.run(config,prepare_overlap=True)
        actual=json.loads((Path(config['workspace'])/'plan.json').read_text())
        self.assertEqual(canonical(expected),canonical(actual))
        self.assertFalse(any(t.name=='apply-prepare' for t in threading.enumerate()))

    def test_initialization_failure_cancels_job_and_never_applies(self):
        self.spool.append(280,300,f.tx(280,300,f.change(b'I',42,new=[2,None,'two'])))
        config=self.config_next('0/12C');started=threading.Event()
        def prepare(config,data,check):
            started.set()
            while True:
                check();time.sleep(.001)
        def initialization(config):
            self.assertTrue(started.wait(5));raise CaptureError('incremental_disk_budget')
        with patch.object(p,'decode',prepare),patch.object(w,'initialize',initialization),patch.object(w,'apply_table') as apply:
            with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):w.run(config,prepare_overlap=True)
            apply.assert_not_called()
        self.assertFalse((Path(config['workspace'])/'result.json').exists())
        self.assertFalse(any(t.name=='apply-prepare' for t in threading.enumerate()))

    def test_production_default_does_not_start_experimental_overlap(self):
        self.spool.append(280,300,f.tx(280,300,f.change(b'I',42,new=[2,None,'two'])))
        config=self.config_next('0/12C')
        with patch.object(p.Preparation,'__enter__',side_effect=AssertionError('experimental stage enabled')):
            self.assertEqual(w.execute(config),0)


class LargeCompactionTests(unittest.TestCase):
    storage_profile='large'
    def setUp(self):
        OverlapTests.setUp(self)
        active=patch.object(maintenance,'run',lambda config:w.run(config,prepare_overlap=True))
        active.start();self.addCleanup(active.stop)
    tearDown=f.IncrementalTests.tearDown
    config_next=f.IncrementalTests.config_next
    config_compact=maintenance.CompactionTests.config_compact
    result=maintenance.CompactionTests.result
    rows=maintenance.CompactionTests.rows
    test_compaction_preserves_exact_old_epoch_and_next_batch_reuses_root=maintenance.CompactionTests.test_compaction_preserves_exact_old_epoch_and_next_batch_reuses_root
    def test_sigkill_at_compaction_and_apply_boundaries_recovers_same_generation(self):
        config=self.config_compact();path=self.root/'config.json';path.write_bytes(canonical(config))
        script='import json,sys;from incremental_worker import run;run(json.load(open(sys.argv[1])),prepare_overlap=True)'
        for point in ('after_compaction_table','before_compaction_rename','after_compaction_rename','after_first_table'):
            env=dict(os.environ,SUPABRICKS_CAPTURE_FAILPOINT=point)
            child=subprocess.run([sys.executable,'-c',script,str(path)],env=env,cwd=Path(w.__file__).parent)
            self.assertEqual(child.returncode,86)
            self.assertEqual(len(self.rows(self.first,42)),1)
            self.assertFalse((Path(config['workspace'])/'result.json').exists())
        w.run(config,prepare_overlap=True);result=self.result(config)
        self.assertEqual(len(self.rows(result,42)),2)
        self.assertEqual(result['manifest']['tables'][0]['version'],1)


if __name__=='__main__':unittest.main()
