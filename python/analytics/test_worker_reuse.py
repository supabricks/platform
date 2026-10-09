"""SP09a request isolation, lifecycle bounds and real multi-epoch Delta replay."""
import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from capture.spool import CaptureError, atomic
from incremental import reuse
from incremental_worker import execute
import test_incremental as fixture
from test_incremental import tx, change


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'input.json'
        self.config=dict(id='a',attempt=1,worker_generation=1,identity={'project':'a'},deadline_ms=time.time()*1000+60000)
        atomic(self.path,self.config)

    def test_same_request_never_executes_twice_and_idle_exits(self):
        calls=[]
        with patch.object(reuse,'IDLE_SECONDS',.03):reuse.serve(self.path,lambda c:calls.append(c['id']) or 0)
        self.assertEqual(calls,['a'])
        self.assertEqual(json.loads(self.path.with_name('done.json').read_text())['id'],'a')

    def test_scope_change_is_rejected_before_execution(self):
        for key,value in [('identity',{'project':'b'}),('worker_generation',2),('storage_generation','other'),
                          ('source_revision',2),('reuse_authority',{'policy_revision':2})]:
            with self.subTest(key=key):
                atomic(self.path,self.config);calls=[]
                def request(c):
                    calls.append(c['id']);atomic(self.path,dict(c,id='b',**{key:value}));return 0
                with self.assertRaisesRegex(CaptureError,'worker_scope_changed'):reuse.serve(self.path,request)
                self.assertEqual(calls,['a'])

    def test_request_limit_and_failure_recycle(self):
        calls=[]
        def request(c):
            calls.append(c['id']);atomic(self.path,dict(c,id=c['id']+'x'));return 0
        with patch.object(reuse,'MAX_REQUESTS',3):reuse.serve(self.path,request)
        self.assertEqual(len(calls),3)
        atomic(self.path,self.config);calls=[]
        reuse.serve(self.path,lambda c:calls.append(c['id']) or 2)
        self.assertEqual(calls,['a'])

    def test_expired_request_never_runs(self):
        atomic(self.path,dict(self.config,deadline_ms=0))
        with self.assertRaisesRegex(CaptureError,'apply_deadline'):reuse.serve(self.path,lambda c:self.fail('executed'))

    def test_crash_has_no_done_receipt(self):
        def crash(c):raise SystemExit(86)
        with self.assertRaises(SystemExit):reuse.serve(self.path,crash)
        self.assertFalse(self.path.with_name('done.json').exists())

    def test_memory_highwater_recycles(self):
        calls=[]
        with patch.object(reuse,'RECYCLE_BYTES',1):reuse.serve(self.path,lambda c:calls.append(c['id']) or 0)
        self.assertEqual(calls,['a'])


class EpochReuseTests(unittest.TestCase):
    def setUp(self):
        previous=os.umask(0o077);self.addCleanup(os.umask,previous)
        fixture.IncrementalTests.setUp(self)
    tearDown=fixture.IncrementalTests.tearDown
    config_next=fixture.IncrementalTests.config_next
    rows=fixture.IncrementalTests.rows

    def test_reused_process_reads_new_epoch_and_releases_mutation_lease(self):
        self.spool.append(280,300,tx(280,300,change(b'I',42,new=[2,None,'two'])))
        config=self.config_next('0/12C');config.update(attempt=1,reuse_worker=True)
        mailbox=self.root/'mailbox';mailbox.mkdir();input=mailbox/'input.json';atomic(input,config)
        process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('incremental_worker.py')),str(input)])
        def cleanup():
            if process.poll() is None:process.kill()
            process.wait(timeout=10)
        self.addCleanup(cleanup)
        def done(request):
            limit=time.monotonic()+15
            while time.monotonic()<limit:
                self.assertIsNone(process.poll(),'worker exited unexpectedly')
                if (mailbox/'done.json').exists():
                    receipt=json.loads((mailbox/'done.json').read_text())
                    if receipt['id']==request['id']:
                        result=json.loads((Path(request['workspace'])/'result.json').read_text())
                        self.assertEqual(result['state'],'ready',result)
                        return result['descriptor']
                time.sleep(.01)
            self.fail('worker timeout')
        second=done(config)
        self.spool.append(380,400,tx(380,400,change(b'U',42,new=[2,None,'updated'])))
        third=dict(config,id='00000000-0000-0000-0000-000000000003',epoch_id='third',ordinal=3,
                   after_lsn='0/12C',target_lsn='0/190',previous=second,workspace=str(self.root/'work3'))
        Path(third['workspace']).mkdir();atomic(input,third)
        last=done(third)
        self.assertEqual(next(r['note'] for r in self.rows(second,42) if r['id']==2),'two')
        self.assertEqual(next(r['note'] for r in self.rows(last,42) if r['id']==2),'updated')
        process.terminate();process.wait(timeout=10)
        # SIGTERM/restart retains a valid published prefix and replays this request.
        self.assertEqual(execute(third),0)
        replay=json.loads((Path(third['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual(self.rows(replay,42),self.rows(last,42))


class LargeEpochReuseTests(EpochReuseTests):
    storage_profile='large'


if __name__=='__main__':unittest.main()
