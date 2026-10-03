"""SP10c native binding contract tests; run with the separately pinned wheel."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from capture.spool import Spool, CaptureError
from capture.journal import ReadBusy
from capture.wal import SpoolBackpressure
from rocks_journal import RocksJournal, MIB, FORMAT


class RocksTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'spool'
        self.identity={'generation':'rocks-contract','decoder_version':1}
        self.patch=patch('capture.sqlite_journal.SQLiteJournal',RocksJournal);self.patch.start();self.spool=None
    def tearDown(self):
        if self.spool:self.spool.close()
        self.patch.stop();self.temp.cleanup()
    def open(self):
        self.spool=Spool(self.root,self.identity);return self.spool
    def seed(self):
        s=self.open();s.establish(100,{});s.set('bootstrap',{'lsn':'0/64'})
        s.append_many([(180,200,b'first'),(280,300,b'x'*MIB),(380,400,b'anchor')]);return s

    def test_replay_restart_and_prune_anchor(self):
        s=self.seed();s.close();s=self.open();self.assertFalse(s.append(380,400,b'anchor'))
        with self.assertRaisesRegex(CaptureError,'replay_mismatch'):s.append(380,400,b'bad')
        self.assertGreater(s.prune('0/190'),MIB);s.backend.compact();s.verify();s.close();s=self.open()
        self.assertEqual(s.prefix()['lsn'],300);self.assertEqual(list(s.transactions(100)),[(400,b'anchor')])
        self.assertFalse(s.append(380,400,b'anchor'));s.append(480,500,b'tail');s.verify()

    def test_one_view_pins_metadata_and_records_across_prune_append_compaction(self):
        s=self.seed()
        with s.backend.snapshot(time.monotonic()+10,0) as view:
            before=list(view.records('0000000000000064','0000000000000190'))
            s.append(480,500,b'new');s.prune('0/190');s.backend.db.flush(True)
            # Native background work may run; the one iterator still sees old rows.
            self.assertEqual(view.metadata()['captured'],400)
            self.assertEqual(list(view.records('0000000000000064','00000000000001f4')),before)
            with self.assertRaises(ReadBusy):s.backend.compact()
        s.backend.compact();s.verify()
        with s.backend.snapshot(time.monotonic()+10,0) as view:self.assertEqual(view.metadata()['captured'],500)

    def test_snapshot_delayed_first_seek_still_pins_at_creation(self):
        s=self.seed()
        with s.backend.snapshot(time.monotonic()+10,0) as view:
            s.append(480,500,b'new');s.prune('0/190')
            self.assertEqual(view.metadata()['captured'],400)
            self.assertEqual(len(list(view.records('0000000000000064','00000000000001f4'))),3)

    def test_reader_cancel_deadline_and_retained_generator_release(self):
        s=self.seed();cancel=[False]
        with s.backend.snapshot(time.monotonic()+10,0,lambda:cancel[0]) as view:
            cursor=view.records('0000000000000064','0000000000000190');next(cursor);cancel[0]=True
            with self.assertRaisesRegex(CaptureError,'journal_read_cancelled'):next(cursor)
        self.assertIsNone(view.iterator);s.backend.compact()
        with s.backend.snapshot(time.monotonic()-1,0) as view:
            with self.assertRaisesRegex(CaptureError,'journal_read_deadline'):view.metadata()

    def test_only_one_reader_and_exclusive_writer(self):
        s=self.seed()
        with self.assertRaisesRegex(CaptureError,'spool_already_owned'):Spool(self.root,self.identity)
        with s.backend.snapshot(time.monotonic()+10,0):
            with self.assertRaises(ReadBusy):
                with s.backend.snapshot(time.monotonic()+10,0):pass

    def test_format_identity_and_sqlite_collision_fail_closed(self):
        s=self.seed();s.close()
        with self.assertRaisesRegex(CaptureError,'spool_identity_mismatch'):Spool(self.root,{'generation':'wrong'})
        (self.root/'spool.sqlite3').touch(mode=0o600)
        with self.assertRaisesRegex(CaptureError,'spool_backend_mismatch'):self.open()
        (self.root/'spool.sqlite3').unlink();(self.root/'format.json').write_text('{}')
        with self.assertRaisesRegex(CaptureError,'spool_backend_mismatch'):self.open()

    def test_corruption_never_authorizes_replay(self):
        s=self.seed();value=s.backend.db[b't:0000000000000190'];s.backend.db[b't:0000000000000190']=value[:-1]+b'!'
        with self.assertRaisesRegex(CaptureError,'spool_corrupt'):s.verify()
        s.close()
        with self.assertRaisesRegex(CaptureError,'spool_corrupt'):self.open()

    def test_group_rejection_does_not_write_any_prefix(self):
        s=self.seed()
        with self.assertRaisesRegex(CaptureError,'noncontiguous_commit_order'):s.append_many([(480,500,b'ok'),(450,600,b'bad')])
        with self.assertRaisesRegex(CaptureError,'group_budget'):s.append_many([(480,500,b'x'*(4*MIB+1))])
        self.assertEqual(s.captured,400);s.verify()

    def test_physical_accounting_and_backpressure(self):
        s=self.seed();s.backend.compact();self.assertEqual(s.physical(),sum(p.stat().st_size for p in s.backend.path.iterdir()))
        s.limit=16*MIB
        with self.assertRaises(SpoolBackpressure):s.append(480,500,b'x'*(2*MIB))
        self.assertEqual(s.captured,400);s.verify()
        self.assertTrue(s.storage_progress()['wal_enabled'])

    def test_pinned_view_resource_cycles_release_and_resume(self):
        s=self.open();s.limit=32*MIB;s.establish(100,{})
        end=100;blocked=False;peak=0
        with s.backend.snapshot(time.monotonic()+30,0) as view:
            self.assertEqual(view.metadata()['captured'],100)
            for cycle in range(128):
                group=[(end+i*2+1,end+i*2+2,b'x'*4096) for i in range(128)]
                try:s.append_many(group)
                except SpoolBackpressure:blocked=True;break
                end+=256;s.prune(f'0/{end:X}');s.backend.db.flush(True)
                peak=max(peak,s.physical());self.assertLess(peak,s.limit)
            # An old empty view need not retain files written after its cut.
            # Repeated prune/flush cycles must remain below the physical limit;
            # pressure may occur, but forcing it is not a correctness condition.
            self.assertGreater(cycle,16)
            self.assertEqual(view.metadata()['captured'],100)
        s.backend.compact();s.verify();s.append(end+1,end+2,b'resumed');s.verify()

    def test_unsafe_file_and_directory_rejected(self):
        s=self.seed();s.close();(self.root/'rocksdb'/'foreign').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):self.open()

    def test_concurrent_reader_and_writer(self):
        s=self.seed();ready=threading.Event();changed=threading.Event();errors=[]
        def read():
            try:
                with s.backend.snapshot(time.monotonic()+10,0) as view:
                    view.metadata();ready.set();self.assertTrue(changed.wait(5))
                    self.assertEqual(view.metadata()['captured'],400)
                    self.assertEqual(len(list(view.records('0000000000000064','00000000000001f4'))),3)
            except BaseException as error:errors.append(error)
        t=threading.Thread(target=read);t.start();self.assertTrue(ready.wait(5))
        s.append(480,500,b'new');s.prune('0/190');changed.set();t.join(5)
        self.assertFalse(t.is_alive());self.assertEqual(errors,[]);s.verify()

    def crash(self,point,operation):
        code='''import sys\nfrom unittest.mock import patch\nfrom capture.spool import Spool\nfrom rocks_journal import RocksJournal\nwith patch('capture.sqlite_journal.SQLiteJournal',RocksJournal):\n s=Spool(sys.argv[1],{'generation':'rocks-contract','decoder_version':1})\n '''+operation+'\n'
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],env=dict(os.environ,SUPABRICKS_CAPTURE_FAILPOINT=point),capture_output=True,text=True)
        self.assertEqual(result.returncode,86,result.stderr)

    def test_crash_before_append_commit_retains_old_cursor(self):
        self.seed().close();self.crash('before_spool_commit',"s.append(480,500,b'new')")
        s=self.open();self.assertEqual(s.captured,400);s.verify()

    def test_crash_after_append_commit_recovers_wal_without_close(self):
        self.seed().close();self.crash('after_spool_commit',"s.append(480,500,b'new')")
        s=self.open();self.assertEqual(s.captured,500);self.assertFalse(s.append(480,500,b'new'));s.verify()

    def test_crash_before_prune_commit_retains_whole_prefix(self):
        self.seed().close();self.crash('before_spool_prune_commit',"s.prune('0/190')")
        s=self.open();self.assertEqual(s.prefix()['lsn'],100);self.assertEqual(len(list(s.transactions(100))),3);s.verify()

    def test_crash_after_prune_commit_recovers_anchor_and_metadata(self):
        self.seed().close();self.crash('after_spool_prune_commit',"s.prune('0/190')")
        s=self.open();self.assertEqual(s.prefix()['lsn'],300);self.assertEqual(list(s.transactions(100)),[(400,b'anchor')]);s.verify()


if __name__=='__main__':unittest.main()
