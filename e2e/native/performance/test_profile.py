import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import worker_profile as p
from profile_trial import Profile
import psutil

class ProfilingTests(unittest.TestCase):
    def setUp(self):p.METRICS.clear();p.ERRORS.clear()
    def test_nested_spans_keep_self_time_and_original_exception(self):
        error=ValueError('secret row content')
        with self.assertRaises(ValueError) as raised:
            with p.Span('outer'):
                with p.Span('inner'):
                    time.sleep(.002);raise error
        self.assertIs(raised.exception,error)
        self.assertGreaterEqual(p.METRICS['outer']['total_ns'],p.METRICS['inner']['total_ns'])
        self.assertLess(p.METRICS['outer']['self_ns'],p.METRICS['outer']['total_ns'])
        self.assertNotIn('secret',json.dumps(p.ERRORS))
        self.assertEqual(p.METRICS['inner']['errors'],1)
    def test_sqlite_busy_is_recorded_without_changing_transaction_behavior(self):
        with tempfile.TemporaryDirectory() as root:
            a=sqlite3.connect(Path(root)/'journal',isolation_level=None)
            b=sqlite3.connect(Path(root)/'journal',isolation_level=None,factory=p.Connection,timeout=.01)
            try:
                a.execute('CREATE TABLE t(x)');a.execute('BEGIN IMMEDIATE')
                with self.assertRaises(sqlite3.OperationalError):b.execute('BEGIN IMMEDIATE')
                self.assertEqual(p.ERRORS[-1]['sqlite_errorcode'],sqlite3.SQLITE_BUSY)
                a.execute('ROLLBACK');b.execute('BEGIN IMMEDIATE');b.execute('INSERT INTO t VALUES (1)');b.execute('COMMIT')
                self.assertEqual(a.execute('SELECT * FROM t').fetchall(),[(1,)])
            finally:a.close();b.close()
    def test_filesystem_counts_are_nested_and_contain_no_paths(self):
        p.WORK.clear();p.LOCAL.fs_counts={}
        with patch.object(p,'COUNT_FS',True):
            count=p.count_filesystem(lambda path:None,'python_stat_calls')
            with p.Span('apply.plan'):
                count('/private/source')
                with p.Span('apply.boundary'):count('/private/source')
            self.assertEqual(p.WORK['apply.plan.python_stat_calls']['total'],2)
            self.assertEqual(p.WORK['apply.boundary.python_stat_calls']['total'],1)
            self.assertNotIn('/private',json.dumps(p.WORK))

    def test_sql_labels_never_contain_literals(self):
        self.assertEqual(p.sql_label("SELECT 'secret'"),'sqlite.SELECT')
        self.assertEqual(p.sql_label('password=secret'),'sqlite.OTHER')
    def test_process_sampling_retains_transient_denial_but_rejects_persistent_denial(self):
        def process(pid):
            obj=Mock(pid=pid)
            obj.cmdline.return_value=['incremental_worker.py'];obj.name.return_value='python'
            obj.cpu_times.return_value=SimpleNamespace(user=1,system=0)
            obj.io_counters.return_value=SimpleNamespace(read_bytes=0,write_bytes=0)
            obj.num_ctx_switches.return_value=SimpleNamespace(voluntary=1,involuntary=0)
            obj.memory_info.return_value=SimpleNamespace(rss=4096);obj.create_time.return_value=1
            return obj
        root,child=process(1),process(2);root.children.return_value=[child]
        profile=Profile.__new__(Profile);profile.cell=SimpleNamespace(daemons=[SimpleNamespace(pid=1)])
        profile.process_sample_errors=[];profile.process_denials={}
        with patch('profile_trial.psutil.Process',return_value=root):
            child.io_counters.side_effect=[psutil.AccessDenied(2),SimpleNamespace(read_bytes=0,write_bytes=0)]
            self.assertEqual(len(profile.processes()),1)
            self.assertEqual(profile.process_sample_errors[0]['error'],'AccessDenied')
            self.assertEqual(len(profile.processes()),2)
            self.assertEqual(profile.process_denials,{})
            child.io_counters.side_effect=psutil.AccessDenied(2)
            profile.processes();profile.processes()
            with self.assertRaises(psutil.AccessDenied):profile.processes()
            root.io_counters.side_effect=psutil.AccessDenied(1)
            with self.assertRaises(psutil.AccessDenied):profile.processes()
    def test_install_connect_factory_is_not_rebound_by_other_hooks(self):
        script="""
import sys,tempfile,select,sqlite3
from pathlib import Path
sys.path.insert(0,'python/analytics')
import worker_profile as p
with tempfile.TemporaryDirectory() as root:
 root=Path(root);(root/'sync-profile').mkdir();(root/'sync-profile/enabled').touch()
 path=root/'capture/id/control.json';path.parent.mkdir(parents=True);path.touch();sys.argv=['worker',str(path)]
 p.install({'select':select},'capture')
 db=sqlite3.connect(':memory:');assert isinstance(db,p.Connection);db.execute('SELECT 1');db.close()
 p.STOP.set();p.flush(final=True)
 assert p.METRICS['sqlite.connect']['calls']==1
 p.ENABLED=False
"""
        result=subprocess.run([sys.executable,'-c',script],env=dict(__import__('os').environ,PYTHONPATH=str(Path(__file__).parent.resolve())),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_reused_process_profiles_each_request_and_keeps_errors_separate(self):
        with tempfile.TemporaryDirectory() as directory, patch.multiple(p,
                ENABLED=True,ROLE='incremental',PROFILE_ROOT=Path(directory),REQUEST_INDEX=0,
                REQUEST_ACTIVE=False,WRITTEN=0,WRITE_ERRORS=0,NATIVE=None):
            def run(config):
                if config['id']=='bad':raise ValueError('private row')
                return config['id']
            measured=p.request_profile(p.wrap(run,'apply.run'))
            self.assertEqual(measured(dict(id='first',attempt=1)),'first')
            with self.assertRaises(ValueError):measured(dict(id='bad',attempt=2))
            self.assertEqual(measured(dict(id='last',attempt=1)),'last')
            rows=[json.loads(path.read_text().splitlines()[-1]) for path in Path(directory).glob('*.jsonl')]
            self.assertEqual({r['context_id'] for r in rows},{'first','bad','last'})
            for r in rows:
                self.assertTrue(r['final']);self.assertEqual(r['metrics']['apply.run']['calls'],1)
                self.assertEqual(r['metrics']['apply.run']['errors'],int(r['context_id']=='bad'))
                self.assertGreaterEqual(r['request_cpu_s'],0)
                self.assertNotIn('private row',json.dumps(r))
            self.assertEqual({r['pid'] for r in rows},{__import__('os').getpid()})
            self.assertEqual({r['worker_request_index'] for r in rows},{1,2,3})
