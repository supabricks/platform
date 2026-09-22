"""Transactional source barriers remain coupled to durable complete commits."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
from capture.spool import Spool,CaptureError
from capture.protocol import Decoder
from incremental.rows import changes
from test_capture import begin,commit
from test_incremental import tx


def message(prefix,run,flags=1):
    data=run.encode()
    return b'M'+struct.pack('!BQ',flags,200)+prefix.encode()+b'\0'+struct.pack('!I',len(data))+data


class TriggeredTests(unittest.TestCase):
    def test_decoder_and_apply_accept_only_generation_owned_transactional_barriers(self):
        run=str(uuid.uuid4());prefix='supabricks.barrier.'+str(uuid.uuid4())
        for actual,flags in [(prefix,1),(prefix,0),(prefix+'foreign',1)]:
            decoder=Decoder({},'ddl',prefix);decoder.feed(begin(280))
            if actual==prefix and flags==1:
                decoder.feed(message(actual,run,flags));c,e,payload=decoder.feed(commit(280,300))
                self.assertEqual(changes(payload,{},e,prefix),[])
                with self.assertRaises(CaptureError):changes(payload,{},e)
            else:
                with self.assertRaises(CaptureError):decoder.feed(message(actual,run,flags))
        decoder=Decoder({},'ddl');decoder.feed(begin(280))
        with self.assertRaises(CaptureError):decoder.feed(message(prefix,run))
    def test_duplicate_emission_keeps_first_boundary_and_journal_verifies_it(self):
        identity=dict(generation=str(uuid.uuid4()),decoder_version=2);run=str(uuid.uuid4());prefix='supabricks.barrier.'+identity['generation']
        with tempfile.TemporaryDirectory() as tmp:
            spool=Spool(Path(tmp)/'spool',identity);spool.establish(100,{})
            spool.append(280,300,tx(280,300,message(prefix,run)))
            spool.append(380,400,tx(380,400,message(prefix,run)))
            expected=dict(run_id=run,end_lsn='0/12C');self.assertEqual(spool.get('barrier'),expected);spool.close()
            spool=Spool(Path(tmp)/'spool',identity);self.assertEqual(spool.get('barrier'),expected)
            spool.set('barrier',dict(run_id=run,end_lsn='0/190'));spool.close()
            with self.assertRaises(CaptureError):Spool(Path(tmp)/'spool',identity)
    def test_barrier_receipt_and_cursor_commit_together_under_process_exit(self):
        identity=dict(generation=str(uuid.uuid4()),decoder_version=2);run=str(uuid.uuid4());prefix='supabricks.barrier.'+identity['generation']
        raw=tx(280,300,message(prefix,run))
        for point in ['before_spool_commit','after_spool_commit']:
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'spool';spool=Spool(path,identity);spool.establish(100,{});spool.close()
                code='import sys,json;from capture.spool import Spool;s=Spool(sys.argv[1],json.loads(sys.argv[2]));s.append(280,300,bytes.fromhex(sys.argv[3]))'
                env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),SUPABRICKS_CAPTURE_FAILPOINT=point)
                child=subprocess.run([sys.executable,'-c',code,str(path),json.dumps(identity),raw.hex()],env=env)
                self.assertEqual(child.returncode,86)
                spool=Spool(path,identity)
                self.assertEqual(spool.captured,100 if point=='before_spool_commit' else 300)
                self.assertEqual(spool.get('barrier'),None if point=='before_spool_commit' else dict(run_id=run,end_lsn='0/12C'))
                spool.append(280,300,raw);self.assertEqual(spool.get('barrier')['end_lsn'],'0/12C');spool.close()

if __name__=='__main__':unittest.main()
