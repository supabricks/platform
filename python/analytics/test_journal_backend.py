"""SP10a persisted-format parity and storage/policy separation."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from capture.spool import Spool, CaptureError
from capture.sqlite_journal import snapshot
from incremental.storage import journal


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'spool'
        self.root.mkdir(mode=0o700);self.identity={'generation':'format-parity','decoder_version':1}
        self.spool=None
    def tearDown(self):
        if self.spool:self.spool.close()
        self.temp.cleanup()

    def legacy_file(self):
        # Frozen pre-SP10 schema and raw SQLite writer: no new backend helpers.
        path=self.root/'spool.sqlite3';path.touch(mode=0o600)
        with sqlite3.connect(path) as db:
            db.executescript('PRAGMA auto_vacuum=INCREMENTAL; CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE transactions(seq INTEGER PRIMARY KEY,end_lsn TEXT NOT NULL UNIQUE,previous_lsn TEXT NOT NULL,commit_lsn TEXT NOT NULL,payload BLOB NOT NULL,sha256 TEXT NOT NULL);')
            values=dict(identity=self.identity,start=100,captured=200,schema={},bytes=6,bootstrap={'lsn':'0/64'})
            db.executemany('INSERT INTO metadata VALUES (?,?)',[(k,json.dumps(v)) for k,v in values.items()])
            db.execute('INSERT INTO transactions VALUES (1,?,?,?,?,?)',('00000000000000c8','0000000000000064','00000000000000b4',b'legacy',hashlib.sha256(b'legacy').hexdigest()))
        return path

    def test_legacy_file_replay_new_append_and_legacy_reader(self):
        path=self.legacy_file();self.spool=Spool(self.root,self.identity)
        self.assertFalse(self.spool.append(180,200,b'legacy'))
        self.spool.append_many([(280,300,b'new'),(380,400,b'tail')]);self.spool.verify()
        with sqlite3.connect(path) as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM metadata WHERE key='captured'").fetchone()[0]),400)
            self.assertEqual(db.execute('SELECT end_lsn,previous_lsn,payload FROM transactions ORDER BY seq').fetchall(),[
                ('00000000000000c8','0000000000000064',b'legacy'),('000000000000012c','00000000000000c8',b'new'),('0000000000000190','000000000000012c',b'tail')])
            self.assertEqual([r[1] for r in db.execute('PRAGMA table_info(transactions)')],['seq','end_lsn','previous_lsn','commit_lsn','payload','sha256'])
        config=dict(spool=str(path),identity=self.identity,bootstrap_lsn='0/64',after_lsn='0/64',target_lsn='0/190',deadline_ms=time.time()*1000+60000)
        self.assertEqual(journal(config)[1],[(200,b'legacy'),(300,b'new'),(400,b'tail')])

    def test_snapshot_cursor_and_metadata_remain_pinned_across_append_and_prune(self):
        self.legacy_file();self.spool=Spool(self.root,self.identity)
        payload=b'x'*(1024*1024);self.spool.append_many([(280,300,payload),(380,400,b'anchor')])
        with snapshot(self.spool.path,time.monotonic()+10,.1) as old:
            self.assertEqual(old.metadata()['captured'],400)
            before=list(old.records('0000000000000064','0000000000000190'))
            self.spool.append(480,500,b'next')
            self.assertGreater(self.spool.prune('0/190'),0)
            self.assertEqual(old.metadata()['captured'],400)
            self.assertEqual(list(old.records('0000000000000064','0000000000000190')),before)
        self.spool.verify()
        with snapshot(self.spool.path,time.monotonic()+10,.1) as new:
            meta=new.metadata();self.assertEqual(meta['captured'],500)
            self.assertEqual(meta['pruned_prefix']['lsn'],300)

    def test_policy_rejects_identity_and_order_before_backend_write(self):
        self.legacy_file();self.spool=Spool(self.root,self.identity)
        with patch.object(self.spool.backend,'append_group') as write:
            with self.assertRaisesRegex(CaptureError,'noncontiguous_commit_order'):
                self.spool.append_many([(280,300,b'ok'),(250,400,b'bad')])
            self.spool.set('identity',{'generation':'other'})
            with self.assertRaisesRegex(CaptureError,'spool_identity_mismatch'):
                self.spool.append(280,300,b'ok')
            write.assert_not_called()

    def test_snapshot_exception_releases_lease_and_all_cursors(self):
        self.legacy_file();self.spool=Spool(self.root,self.identity,journal_mode='delete')
        retained=None
        try:
            with snapshot(self.spool.path,time.monotonic()+10,.1) as view:
                view.metadata();cursor=view.records('0000000000000064','00000000000000c8')
                next(cursor);raise RuntimeError('retain traceback')
        except RuntimeError as error:retained=error
        self.assertIsNotNone(retained.__traceback__)
        with sqlite3.connect(self.spool.path,timeout=0) as writer:writer.execute('BEGIN EXCLUSIVE')


if __name__=='__main__':unittest.main()
