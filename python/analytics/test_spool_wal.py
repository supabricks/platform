"""Real WAL ownership, migration, reader pin, quota and stopped-copy boundaries."""
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from capture.spool import CaptureError, Spool
from capture.wal import SpoolBackpressure, private_sizes, reader_lease
from capture.groups import Groups
from incremental.storage import journal

IDENTITY = dict(generation='wal-test', decoder_version=1)
LIMIT = 16*1024*1024


class WalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'spool';self.spool=None
    def tearDown(self):
        if self.spool:self.spool.close()
        self.temp.cleanup()
    def open(self, mode='wal'):
        self.spool=Spool(self.root,IDENTITY,LIMIT,journal_mode=mode)
        self.spool.establish(100,{})
        return self.spool

    def test_checked_full_policy_and_read_only_snapshot_does_not_block_append(self):
        s=self.open();s.set('bootstrap',dict(lsn='0/64'))
        self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0],'wal')
        self.assertEqual(s.db.execute('PRAGMA synchronous').fetchone()[0],2)
        self.assertEqual(s.db.execute('PRAGMA wal_autocheckpoint').fetchone()[0],0)
        self.assertEqual(s.db.execute('PRAGMA cache_spill').fetchone()[0],0)
        db=sqlite3.connect(s.path.as_uri()+'?mode=ro',uri=True)
        try:
            db.execute('BEGIN');self.assertEqual(db.execute('SELECT count(*) FROM transactions').fetchone()[0],0)
            s.append(180,200,b'committed')
            self.assertEqual(db.execute('SELECT count(*) FROM transactions').fetchone()[0],0)
            db.rollback()
            self.assertEqual(db.execute('SELECT count(*) FROM transactions').fetchone()[0],1)
            with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM transactions')
        finally:db.close()
        import time
        config=dict(spool=str(s.path),identity=IDENTITY,bootstrap_lsn='0/64',after_lsn='0/64',target_lsn='0/C8',deadline_ms=time.time()*1000+5000)
        self.assertEqual(journal(config)[1],[(200,b'committed')])
        # The applier must not retain a snapshot/lease after materialization.
        lease=os.open(self.root/'readers.lock',os.O_RDWR)
        try:fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(lease)
        self.assertEqual(s.journal.checkpoint(force=True),(0,0,0))

    def test_pinned_reader_causes_bounded_backpressure_then_recovers_same_group(self):
        s=self.open();db=sqlite3.connect(s.path.as_uri()+'?mode=ro',uri=True)
        db.execute('BEGIN');db.execute('SELECT * FROM metadata').fetchall()
        groups=Groups(s,count=1);end=100;blocked=None
        try:
            for _ in range(2000):
                groups.add((end+1,end+2,b'x'*1024))
                try:groups.flush()
                except SpoolBackpressure:
                    blocked=end+2;break
                end+=2
            self.assertIsNotNone(blocked)
            self.assertEqual(s.captured,end)
            self.assertEqual(len(groups.pending),1)
            self.assertLessEqual(s.journal.physical(),LIMIT)
            self.assertGreater(s.journal.stats['busy'],0)
            self.assertEqual(s.journal.checkpoint(force=True)[0],1)
        finally:db.close()
        groups.flush()
        self.assertEqual(s.captured,blocked)
        self.assertFalse(groups.pending)
        self.assertLessEqual(s.journal.physical(),LIMIT)
        s.verify()

    def test_mode_changes_wait_for_reader_lease_and_preserve_committed_prefix(self):
        s=self.open('delete');s.append(180,200,b'first');s.close()
        s=self.open();s.append(280,300,b'wal-only')
        lease=reader_lease(s.path)
        s.close()
        try:
            with self.assertRaisesRegex(CaptureError,'spool_migration_busy'):
                Spool(self.root,IDENTITY,LIMIT,journal_mode='delete')
        finally:os.close(lease)
        s=self.open('delete')
        self.assertEqual(s.captured,300);self.assertEqual(list(s.transactions(100)),[(200,b'first'),(300,b'wal-only')])
        self.assertEqual(s.db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
        s.close();s=self.open();self.assertEqual(s.captured,300)

    def test_legacy_unleased_sqlite_reader_also_prevents_downgrade(self):
        s=self.open();s.append(180,200,b'first')
        db=sqlite3.connect(s.path.as_uri()+'?mode=ro',uri=True)
        db.execute('BEGIN');db.execute('SELECT * FROM metadata').fetchall()
        s.append(280,300,b'newer');s.close()
        try:
            with self.assertRaisesRegex(CaptureError,'spool_migration_busy'):
                Spool(self.root,IDENTITY,LIMIT,journal_mode='delete')
        finally:db.close()
        s=self.open('delete');self.assertEqual(s.captured,300)

    def test_wrong_identity_never_changes_wal_mode_or_discards_wal(self):
        s=self.open();s.append(180,200,b'kept');s.close()
        wal=Path(str(s.path)+'-wal');before=hashlib.sha256(wal.read_bytes()).hexdigest()
        with self.assertRaisesRegex(CaptureError,'spool_identity_mismatch'):
            Spool(self.root,dict(IDENTITY,generation='wrong'),LIMIT,journal_mode='delete')
        self.assertEqual(hashlib.sha256(wal.read_bytes()).hexdigest(),before)
        self.assertEqual(self.open().captured,200)

    def test_stopped_copy_preserves_committed_wal_then_sqlite_downgrades(self):
        s=self.open();s.append(180,200,b'wal-only');s.close()
        self.assertGreater(Path(str(s.path)+'-wal').stat().st_size,0)
        restored=Path(self.temp.name)/'restored';shutil.copytree(self.root,restored)
        other=Spool(restored,IDENTITY,LIMIT,journal_mode='delete')
        try:
            self.assertEqual(other.captured,200);other.verify()
            self.assertEqual(list(other.transactions(100)),[(200,b'wal-only')])
        finally:other.close()

    def test_sidecar_symlinks_hardlinks_and_public_permissions_fail_before_open(self):
        for suffix in ('-wal','-shm','-journal'):
            for kind in ('symlink','hardlink','public'):
                with self.subTest(suffix=suffix,kind=kind):
                    root=Path(self.temp.name)/(suffix+kind);root.mkdir(mode=0o700)
                    victim=root/'victim';victim.write_bytes(b'unchanged');victim.chmod(0o600)
                    path=root/('spool.sqlite3'+suffix)
                    if kind=='symlink':path.symlink_to(victim)
                    elif kind=='hardlink':os.link(victim,path)
                    else:path.write_bytes(b'unsafe');path.chmod(0o644)
                    with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):Spool(root,IDENTITY,LIMIT)
                    self.assertEqual(victim.read_bytes(),b'unchanged')

    def test_all_database_sidecars_are_counted_and_temp_spilling_disabled(self):
        s=self.open();s.append(180,200,b'payload')
        sizes=private_sizes(s.path);progress=s.journal.progress()
        self.assertEqual(progress['physical_bytes'],sum(sizes.values()))
        self.assertGreater(progress['physical_bytes'],s.path.stat().st_size)
        self.assertEqual(s.db.execute('PRAGMA temp_store').fetchone()[0],2)
        with patch('capture.wal.os.statvfs') as disk:
            disk.return_value.f_bavail=0;disk.return_value.f_frsize=4096
            with self.assertRaises(SpoolBackpressure):s.append(280,300,b'not admitted')
        self.assertEqual(s.captured,200)

    def test_process_death_at_migration_and_checkpoint_boundaries(self):
        import capture.spool as implementation
        for point in ('before_spool_migration','after_spool_migration','before_spool_checkpoint','after_spool_checkpoint'):
            for mode in ('wal','delete'):
                with self.subTest(point=point,mode=mode):
                    root=Path(self.temp.name)/(point+mode)
                    previous='delete' if mode=='wal' else 'wal'
                    s=Spool(root,IDENTITY,LIMIT,journal_mode=previous)
                    s.establish(100,{});s.append(180,200,b'committed');s.close()
                    # Checkpoint faults exercise a live WAL with a committed suffix.
                    code="import sys;from capture.spool import Spool;s=Spool(sys.argv[1],"+repr(IDENTITY)+","+str(LIMIT)+",journal_mode="+repr(mode)+");s.journal.checkpoint(force=True)"
                    if 'checkpoint' in point and mode=='wal':
                        s=Spool(root,IDENTITY,LIMIT);s.close()
                    env=dict(os.environ,PYTHONPATH=str(Path(implementation.__file__).resolve().parents[1]),SUPABRICKS_CAPTURE_FAILPOINT=point)
                    child=subprocess.run([sys.executable,'-B','-c',code,str(root)],env=env,timeout=10)
                    self.assertEqual(child.returncode,86)
                    s=Spool(root,IDENTITY,LIMIT)
                    try:
                        self.assertEqual(s.captured,200);s.verify()
                        self.assertEqual(list(s.transactions(100)),[(200,b'committed')])
                        self.assertLessEqual(s.journal.physical(),LIMIT)
                    finally:s.close()

    def test_low_disk_migration_retains_original_mode_and_committed_prefix(self):
        s=self.open('delete');s.append(180,200,b'kept');s.close()
        with patch('capture.wal.os.statvfs') as disk:
            disk.return_value.f_bavail=0;disk.return_value.f_frsize=4096
            with self.assertRaises(SpoolBackpressure):Spool(self.root,IDENTITY,LIMIT)
        with sqlite3.connect(s.path.as_uri()+'?mode=ro',uri=True) as db:
            self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
        s=self.open();self.assertEqual(s.captured,200);s.verify()

    def test_unqualified_sqlite_is_rejected_before_a_wal_connection_opens(self):
        for mode in ('wal','delete'):
            with patch('capture.wal.fixed_sqlite',return_value=False),patch('capture.spool.sqlite3.connect') as connect:
                with self.assertRaisesRegex(CaptureError,'sqlite_wal_unqualified'):self.open(mode)
            connect.assert_not_called()


if __name__=='__main__':unittest.main()
