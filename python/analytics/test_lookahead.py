"""Real process/owner transport plus stale handoff, row semantics and replay."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import uuid
from capture import owner
from capture.spool import atomic,canonical,CaptureError
from incremental import lookahead as p
import incremental_worker as w
import test_incremental as f
import test_journal_owner as journal_fixture


class PreparationAuthorityTests(unittest.TestCase):
    setUp=journal_fixture.OwnerTests.setUp
    tearDown=journal_fixture.OwnerTests.tearDown
    request=journal_fixture.OwnerTests.request
    def test_preparation_needs_separate_exact_daemon_authorization(self):
        config=dict(self.config,preparation=1)
        with self.assertRaises(CaptureError):self.request(config)
        work=self.root/'analytics/prepare-work'/self.run
        work.parent.mkdir(mode=0o700);work.mkdir(mode=0o700)
        atomic(work/'input.json',config)
        self.assertEqual(self.request(config)[1],[(200,b'first'),(300,b'second')])
        for field,value in [('target_lsn','0/190'),('after_lsn','0/C8'),('attempt',2),('preparation',2)]:
            with self.subTest(field=field),self.assertRaises(CaptureError):self.request(dict(config,**{field:value}))
        (work/'input.json').unlink()
        with self.assertRaises(CaptureError):self.request(config)


class LookaheadTests(unittest.TestCase):
    storage_profile='large'
    def setUp(self):
        mask=os.umask(0o077);self.addCleanup(os.umask,mask)
        f.IncrementalTests.setUp(self)
        # A short socket path also fits macOS's sockaddr_un limit.
        for path in ('tmp','capture','analytics/prepare-work'):(self.root/path).mkdir(mode=0o700,exist_ok=True)
        control=self.root/'capture'/self.config['identity']['generation']/'control.json'
        control.parent.mkdir(mode=0o700)
        self.access=dict(endpoint=str(self.root/'tmp'/self.config['identity']['generation']/'journal.sock'),source_revision=1,policy_revision=1)
        self.control=control
        atomic(control,dict(identity=self.config['identity'],worker_generation=1,desired='running',journal_access=self.access))
        self.server=owner.Owner(control,self.spool.backend)
    def tearDown(self):
        self.server.close()
        f.IncrementalTests.tearDown(self)
    config_next=f.IncrementalTests.config_next
    rows=f.IncrementalTests.rows

    def prepared(self,config):
        id=str(uuid.uuid4());work=self.root/'analytics/prepare-work'/id;work.mkdir(mode=0o700)
        issued=dict(preparation=1,id=id,attempt=1,epoch_id=config['previous']['epoch_id'],
            workspace=str(work),schema_sha256=hashlib.sha256(canonical(f.PROFILE)).hexdigest(),
            **{k:config[k] for k in p.SCOPE+('after_lsn','target_lsn','deadline_ms')})
        atomic(work/'input.json',issued)
        child=subprocess.run([sys.executable,'-B',str(Path(w.__file__).with_name('prepare_worker.py')),str(work/'input.json')],capture_output=True)
        self.assertEqual(child.returncode,0,child.stderr.decode())
        receipt=json.loads((work/'result.json').read_text())
        work.rename(Path(config['workspace'])/'prepared')
        config['prepared_batch']=dict(authorization=issued,receipt=receipt)
        return receipt

    def next(self):
        config=self.config_next('0/12C');config['journal_access']=self.access
        return config

    def changes(self):
        self.spool.append(280,300,f.tx(280,300,
            f.change(b'U',42,old=[1,None,None],new=[2,'12.34567890',f.UNCHANGED]),
            f.change(b'I',43,new=[2,None,'日本語']),
            f.change(b'D',43,old=[1,None,None])))

    def test_process_result_matches_serial_plan_toast_keys_deletes_and_replay(self):
        self.changes();config=self.next()
        # Direct fixture read supplies the independent serial plan before the
        # owner-only production config is attached.
        direct=dict(config);direct.pop('journal_access')
        expected=w.plan(config,Path(config['generation']),self.first,w.journal(direct))
        receipt=self.prepared(config);self.assertEqual(receipt['rows'],3)
        with patch.object(w,'journal',side_effect=AssertionError('prepared result reread journal')):
            def fail(point):
                if point=='after_table_commit':raise SystemExit(86)
            with patch.object(w,'fault',fail),self.assertRaises(SystemExit):w.run(config)
            self.assertEqual(canonical(expected),canonical(json.loads((Path(config['workspace'])/'plan.json').read_text())))
            # Recovery uses the sealed plan even if all ephemeral files vanish.
            import shutil
            shutil.rmtree(Path(config['workspace'])/'prepared')
            w.run(config)
        result=json.loads((Path(config['workspace'])/'result.json').read_text())['descriptor']
        self.assertEqual(self.rows(result,42),[dict(id=2,amount='12.34567890',note='original')])
        self.assertEqual(self.rows(result,43),[dict(id=2,amount=None,note='日本語')])

    def test_stale_identity_predecessor_generation_policy_target_and_checksum_discard(self):
        self.changes();config=self.next();self.prepared(config)
        mutations=[('worker_generation',2),('source_revision',2),('target_lsn','0/110'),('after_lsn','0/D0'),
            ('identity',dict(config['identity'],generation=str(uuid.uuid4()))),
            ('journal_access',dict(self.access,policy_revision=2)),('storage_profile','compact'),
            ('previous',dict(config['previous'],epoch_id=str(uuid.uuid4())))]
        for field,v in mutations:
            changed=copy.deepcopy(config);changed[field]=v
            with self.subTest(field=field):self.assertIsNone(p.optional_consume(changed))
        self.assertEqual(p.consume(config).end,300)
        path=Path(config['workspace'])/'prepared/batch.jsonl'
        path.write_bytes(path.read_bytes().replace(b'12.34567890',b'12.34567891'))
        self.assertIsNone(p.optional_consume(config))

    def test_discard_falls_back_before_initialization(self):
        self.changes();config=self.next();self.prepared(config)
        config['prepared_batch']['authorization']['worker_generation']=2
        direct=dict(config);direct.pop('journal_access')
        with patch.object(w,'journal',return_value=w.journal(direct)) as journal:
            w.run(config)
        journal.assert_called_once()
        self.assertEqual(config['_preparation']['outcome'],'discarded')

    def test_preparation_does_not_import_arrow_or_delta(self):
        script="import sys;from incremental.lookahead import prepare;assert 'pyarrow' not in sys.modules and 'deltalake' not in sys.modules"
        subprocess.run([sys.executable,'-B','-c',script],cwd=Path(w.__file__).parent,check=True)

    def test_byte_and_row_limits_reject_before_consumption(self):
        self.changes();config=self.next();self.prepared(config)
        for key,value in [('rows',65537),('bytes',p.MAX_BYTES+1),('input_bytes',16*1024*1024+1)]:
            changed=copy.deepcopy(config);changed['prepared_batch']['receipt'][key]=value
            self.assertIsNone(p.optional_consume(changed))


if __name__=='__main__':unittest.main()
