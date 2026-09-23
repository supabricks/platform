"""Continuous observation derives data backlog from durable commits, not WAL end."""
from pathlib import Path
import tempfile
import unittest
import uuid
from capture.spool import Spool
from test_incremental import tx
from test_triggered import message
from test_capture import insert

class ContinuousTests(unittest.TestCase):
    def test_idle_barriers_do_not_become_data_backlog_and_recovery_rebuilds_progress(self):
        identity=dict(generation=str(uuid.uuid4()),decoder_version=2)
        prefix='supabricks.barrier.'+identity['generation'];run=str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'spool';s=Spool(path,identity);s.establish(100,{})
            s.append(180,200,tx(180,200,insert()))
            s.append(280,300,tx(280,300,message(prefix,run)))
            self.assertEqual(s.progress('0/C8')['last_data_lsn'],'0/C8')
            self.assertIsNone(s.progress('0/C8')['oldest_commit_at_ms'])
            self.assertEqual(s.progress('0/C8')['barrier_commit_at_ms'],946684800000)
            self.assertGreater(s.progress('0/C8')['backlog_bytes'],0)
            s.append(380,400,tx(380,400,insert()))
            expected=s.progress('0/C8');self.assertEqual(expected['last_data_lsn'],'0/190')
            self.assertEqual(expected['oldest_commit_at_ms'],946684800000)
            s.close();s=Spool(path,identity);self.assertEqual(s.progress('0/C8'),expected);s.close()

if __name__=='__main__':unittest.main()
