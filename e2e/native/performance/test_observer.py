"""Keep real capture failures distinct from bounded status replacement races."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from trial import Observer


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.capture=self.root/'capture/capture';(self.capture/'spool').mkdir(parents=True)
        with sqlite3.connect(self.capture/'spool/spool.sqlite3') as db:
            db.execute('CREATE TABLE transactions(seq INTEGER,end_lsn TEXT,payload BLOB)')
            db.execute('INSERT INTO transactions VALUES (1,?,?)',('0000000000000064',b'x'*21+b'\0\0\0\1'))
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            db.execute('CREATE TABLE publications(ordinal INTEGER,published_at_ms INTEGER,descriptor TEXT,export_id TEXT,state TEXT)')
            db.execute('CREATE TABLE sync_policies(id TEXT,record TEXT)')
            db.execute('CREATE TABLE sync_captures(id TEXT,record TEXT)')
            db.execute('INSERT INTO sync_policies VALUES (?,?)',('policy','{}'))
            db.execute('INSERT INTO sync_captures VALUES (?,?)',('capture','{}'))
        self.observer=Observer(SimpleNamespace(root=self.root,policy_id='policy'),'capture')

    def test_capture_failure_is_retained_when_status_file_is_absent(self):
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            db.execute('UPDATE sync_policies SET record=?',(json.dumps({'error':'triggered_run_fenced_or_expired'}),))
            db.execute('UPDATE sync_captures SET record=?',(json.dumps({'state':'resync_required','desired':'fenced','error':'group_budget'}),))
        self.observer.poll()
        self.assertEqual(self.observer.runtime_error,'group_budget')
        self.assertEqual(self.observer.ends,{1:100});self.assertEqual(self.observer.missing_status_samples,0)

    def test_capture_failure_before_policy_error_is_found_on_missing_status(self):
        with sqlite3.connect(self.root/'state.sqlite3') as db:
            db.execute('UPDATE sync_captures SET record=?',(json.dumps({'state':'resync_required','desired':'fenced','error':'group_budget'}),))
        self.observer.poll()
        self.assertEqual(self.observer.runtime_error,'group_budget')
        self.assertEqual(self.observer.missing_status_samples,0)

    def test_transient_status_omission_keeps_markers_and_recovers(self):
        self.observer.poll();self.assertEqual(self.observer.ends,{1:100})
        self.assertEqual(self.observer.missing_status_samples,1)
        (self.capture/'status.json').write_text(json.dumps({'captured_lsn':'0/64','progress':{}}))
        original=Path.read_text
        def read(path,*args,**kwargs):
            if str(path)=='/sys/fs/cgroup/memory.current':return '1024'
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',read):self.observer.poll()
        self.assertEqual(self.observer.missing_status_streak,0)
        self.assertEqual(self.observer.series[-1]['captured_lsn'],'0/64')

    def test_missing_status_is_bounded_and_invalid_json_still_fails(self):
        for _ in range(9):self.observer.poll()
        with self.assertRaisesRegex(RuntimeError,'persistently missing'):self.observer.poll()
        (self.capture/'status.json').write_text('{')
        with self.assertRaises(json.JSONDecodeError):self.observer.poll()

    def test_missing_spool_is_not_tolerated_as_status_replacement(self):
        self.observer.spool.unlink()
        with self.assertRaises(sqlite3.OperationalError):self.observer.poll()
        self.assertEqual(self.observer.missing_status_samples,0)

if __name__=='__main__':unittest.main()
