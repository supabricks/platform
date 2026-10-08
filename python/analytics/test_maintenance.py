"""SY07 durable pruning, generation rollover and retained-reader recovery."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch
from capture.spool import CaptureError, Spool, canonical, pg_lsn
from incremental_worker import run
import test_incremental as fixture


class PruningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'spool'
        self.spool=Spool(self.root,{'decoder_version':1},16*1024*1024)
        self.spool.establish(100,{})
        for i in range(300):self.spool.append(101+2*i,102+2*i,b'x'*8192)
    def tearDown(self):self.spool.close();self.tmp.cleanup()
    def test_published_prefix_only_with_reconnect_anchor_and_reopen(self):
        self.assertEqual(self.spool.prune(None),0)
        self.assertGreater(self.spool.prune(pg_lsn(500)),0)
        self.assertEqual(self.spool.captured,700)
        self.assertEqual(len(list(self.spool.transactions(0))),101)
        self.assertFalse(self.spool.append(499,500,b'x'*8192))
        with self.assertRaisesRegex(CaptureError,'replay_mismatch'):self.spool.append(499,500,b'changed')
        with self.assertRaisesRegex(CaptureError,'published_cursor_regressed'):self.spool.prune(pg_lsn(100))
        self.spool.close();self.spool=Spool(self.root,{'decoder_version':1})
        self.assertEqual(self.spool.captured,700)
        self.assertEqual(self.spool.get('bytes'),101*8192)
    def test_crash_boundaries_leave_one_verified_chain(self):
        self.spool.close()
        for point in ('before_spool_prune_commit','after_spool_prune_commit','after_spool_prune_vacuum'):
            code='from capture.spool import Spool;import sys;s=Spool(sys.argv[1],{"decoder_version":1});s.prune("0/2BC")'
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),SUPABRICKS_CAPTURE_FAILPOINT=point)
            self.assertEqual(subprocess.run([sys.executable,'-c',code,str(self.root)],env=env).returncode,86)
            self.spool=Spool(self.root,{'decoder_version':1});self.spool.verify()
            self.assertEqual(self.spool.captured,700)
            self.assertFalse(self.spool.append(699,700,b'x'*8192))
            # Reset only this test fixture to its original verified chain.
            self.spool.close()
            if point!='after_spool_prune_vacuum':
                import shutil
                shutil.rmtree(self.root)
                self.spool=Spool(self.root,{'decoder_version':1});self.spool.establish(100,{})
                for i in range(300):self.spool.append(101+2*i,102+2*i,b'x'*8192)
                self.spool.close()
    def test_busy_reader_defers_prune_without_fencing_or_losing_rows(self):
        self.spool.close()
        self.spool=Spool(self.root,{'decoder_version':1},journal_mode='delete')
        reader=sqlite3.connect(self.spool.path);reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM transactions').fetchone()
        started=time.monotonic()
        self.assertEqual(self.spool.prune('0/2BC'),0)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.spool.backend.db.execute('PRAGMA busy_timeout').fetchone()[0],50)
        self.spool.verify();self.assertEqual(self.spool.get('bytes'),300*8192)
        reader.close()
        self.assertGreater(self.spool.prune('0/2BC'),0);self.spool.verify()
    def test_sqlite_full_rolls_back_without_acknowledging_partial_transaction(self):
        pages=self.spool.backend.db.execute('PRAGMA page_count').fetchone()[0]
        self.spool.backend.db.execute(f'PRAGMA max_page_count={pages+1}')
        with self.assertRaises(sqlite3.OperationalError) as error:self.spool.append(701,702,b'x'*262144)
        self.assertEqual(error.exception.sqlite_errorcode,sqlite3.SQLITE_FULL)
        self.assertEqual(self.spool.captured,700);self.spool.verify()
    def test_bootstrap_publication_can_precede_first_captured_transaction(self):
        spool=Spool(Path(self.tmp.name)/'idle',{'decoder_version':1})
        try:
            spool.establish(100,{});spool.set('bootstrap',dict(lsn='0/C8'))
            self.assertEqual(spool.prune('0/C8'),0)
            self.assertEqual(spool.captured,100)
            with self.assertRaisesRegex(CaptureError,'published_cursor_ahead'):spool.prune('0/12C')
        finally:spool.close()
    def test_corrupt_prefix_fails_closed_and_reclaimed_pages_are_reusable(self):
        for round in range(4):
            self.spool.prune(pg_lsn(self.spool.captured))
            for _ in range(64):
                end=self.spool.captured;self.spool.append(end+1,end+2,b'x'*65536)
        self.assertLess(self.spool.path.stat().st_size,16*1024*1024)
        value=self.spool.get('pruned_prefix');value['lsn']+=2;self.spool.set('pruned_prefix',value)
        with self.assertRaisesRegex(CaptureError,'spool_corrupt'):self.spool.verify()

    def test_pruned_barrier_preserves_progress_and_old_apply_cursor_is_rejected(self):
        from test_triggered import message
        from test_capture import insert
        from incremental.storage import journal
        identity=dict(generation=str(uuid.uuid4()),decoder_version=2)
        spool=Spool(Path(self.tmp.name)/'barriers',identity)
        try:
            spool.establish(100,{});spool.set('bootstrap',dict(lsn='0/C8'))
            spool.append(180,200,fixture.tx(180,200,message('supabricks.barrier.'+identity['generation'],str(uuid.uuid4()))))
            for end in range(300,2300,100):spool.append(end-20,end,fixture.tx(end-20,end,insert(b'x'*65536)))
            expected=spool.progress(pg_lsn(2200))
            spool.backend.db.execute("DELETE FROM metadata WHERE key='barrier_at_ms'") # Pre-SY07 spool.
            self.assertGreater(spool.prune(pg_lsn(2200)),0)
            self.assertEqual(spool.progress(pg_lsn(2200)),expected);spool.verify()
            config=dict(spool=str(spool.path),identity=identity,bootstrap_lsn='0/C8',after_lsn='0/C8',target_lsn=pg_lsn(2200),deadline_ms=int(time.time()*1000)+60000)
            with self.assertRaisesRegex(CaptureError,'source_history_lost'):journal(config)
            spool.append(2280,2300,fixture.tx(2280,2300,insert()))
            config.update(after_lsn=pg_lsn(2200),target_lsn=pg_lsn(2300))
            self.assertEqual(len(journal(config)[1]),1)
            prefix=spool.get('pruned_prefix');prefix['sha256']='bad';spool.set('pruned_prefix',prefix)
            with self.assertRaisesRegex(CaptureError,'spool_corrupt'):journal(config)
        finally:spool.close()


class MaintenanceBudgetTests(unittest.TestCase):
    def test_normal_source_rate_no_longer_accumulates_published_history(self):
        with tempfile.TemporaryDirectory() as temp:
            retained={}
            for mode in ('prune','maintain'):
                spool=Spool(Path(temp)/mode,{'decoder_version':1})
                try:
                    spool.establish(100,{})
                    # Twenty source-check intervals at 625 atomic transactions/s
                    # (1250 changed rows/s). Freeze only the scheduling clock so
                    # a slow test host cannot change the record-budget contract.
                    for tick in range(20):
                        for first in range(0,625,32):
                            begin=spool.captured
                            spool.append_many([(begin+2*i+1,begin+2*i+2,b'x'*128)
                                               for i in range(min(32,625-first))])
                        with patch('capture.spool.time.monotonic',return_value=0):
                            getattr(spool,mode)(pg_lsn(spool.captured))
                    retained[mode]=len(list(spool.transactions(0)))
                    self.assertEqual(spool.captured,25100)
                    self.assertFalse(spool.append(25099,25100,b'x'*128))
                    spool.verify()
                finally:spool.close()
                reopened=Spool(Path(temp)/mode,{'decoder_version':1})
                try:reopened.verify();self.assertEqual(reopened.captured,25100)
                finally:reopened.close()
            self.assertEqual(retained['prune'],7380)
            self.assertLessEqual(retained['maintain'],256)

    def test_record_time_and_no_progress_budgets_are_independent(self):
        spool=object.__new__(Spool)
        with patch.object(spool,'prune',return_value=10) as prune:
            with patch('capture.spool.time.monotonic',return_value=0):
                self.assertEqual(spool.maintain('0/64'),80)
            self.assertEqual(prune.call_count,8)
            prune.reset_mock()
            with patch('capture.spool.time.monotonic',side_effect=[0,0,.051]):
                self.assertEqual(spool.maintain('0/64'),10)
            self.assertEqual(prune.call_count,1)
            prune.reset_mock();prune.return_value=0
            with patch('capture.spool.time.monotonic',return_value=0):
                self.assertEqual(spool.maintain('0/64'),0)
            self.assertEqual(prune.call_count,1)
            prune.side_effect=CaptureError('spool_corrupt')
            with self.assertRaisesRegex(CaptureError,'spool_corrupt'):spool.maintain('0/64')

    def test_multiple_units_preserve_unpublished_rows_and_reader_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            spool=Spool(Path(temp)/'spool',{'decoder_version':1})
            reader=None
            try:
                spool.establish(100,{})
                for chunk in range(40):
                    begin=spool.captured
                    spool.append_many([(begin+2*i+1,begin+2*i+2,b'x'*128) for i in range(32)])
                reader=sqlite3.connect(spool.path);reader.execute('BEGIN')
                self.assertEqual(reader.execute('SELECT count(*) FROM transactions').fetchone()[0],1280)
                with patch('capture.spool.time.monotonic',return_value=0):spool.maintain(pg_lsn(2100))
                self.assertEqual(reader.execute('SELECT count(*) FROM transactions').fetchone()[0],1280)
                self.assertEqual(len(list(spool.transactions(2100))),280)
                self.assertEqual(spool.captured,2660)
                self.assertFalse(spool.append(2099,2100,b'x'*128))
                spool.verify()
            finally:
                if reader:reader.close()
                spool.close()


class CompactionTests(unittest.TestCase):
    setUp=fixture.IncrementalTests.setUp
    tearDown=fixture.IncrementalTests.tearDown
    config_next=fixture.IncrementalTests.config_next
    def rows(self,descriptor,oid):
        saved=self.config['generation'];self.config['generation']=str(self.root/descriptor['generation'])
        try:return fixture.IncrementalTests.rows(self,descriptor,oid)
        finally:self.config['generation']=saved
    def config_compact(self):
        self.spool.append(280,300,fixture.tx(280,300,fixture.change(b'I',42,new=[2,None,'two'])))
        config=self.config_next('0/12C');generation=str(uuid.uuid4())
        config.update(storage_generation=generation,previous_generation=self.config['generation'],
            generation=str(self.root/'analytics/incremental'/generation))
        return config
    def result(self,config):return json.loads((Path(config['workspace'])/'result.json').read_bytes())['descriptor']
    def test_compaction_preserves_exact_old_epoch_and_next_batch_reuses_root(self):
        config=self.config_compact();old=Path(self.config['generation'])
        before={str(p.relative_to(old)):hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob('*') if p.is_file()}
        run(config);new=self.result(config)
        self.assertEqual(new['manifest']['storage_generation'],config['storage_generation'])
        self.assertEqual(new['manifest']['compaction']['rows'],3)
        self.assertEqual(self.rows(self.first,42)[0]['amount'],'12345678901234567890.12345678')
        self.assertEqual(len(self.rows(self.first,42)),1);self.assertEqual(len(self.rows(new,42)),2)
        self.assertEqual(before,{str(p.relative_to(old)):hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob('*') if p.is_file()})
        self.spool.append(380,400,fixture.tx(380,400,fixture.change(b'D',42,old=[2,None,None])))
        next_config=dict(config,id=str(uuid.uuid4()),epoch_id=str(uuid.uuid4()),previous=new,
            previous_generation=config['generation'],after_lsn='0/12C',target_lsn='0/190',workspace=str(self.root/'work3'))
        Path(next_config['workspace']).mkdir();run(next_config);third=self.result(next_config)
        self.assertIsNone(third['manifest']['compaction'])
        self.assertEqual(len(self.rows(third,42)),1);self.assertEqual(len(self.rows(new,42)),2)
    def test_sigkill_at_compaction_and_apply_boundaries_recovers_same_generation(self):
        config=self.config_compact();path=self.root/'config.json';path.write_bytes(canonical(config))
        for point in ('after_compaction_table','before_compaction_rename','after_compaction_rename','after_first_table'):
            env=dict(os.environ,SUPABRICKS_CAPTURE_FAILPOINT=point)
            self.assertEqual(subprocess.run([sys.executable,str(Path(__file__).with_name('incremental_worker.py')),str(path)],env=env).returncode,86)
            self.assertEqual(len(self.rows(self.first,42)),1)
            self.assertFalse((Path(config['workspace'])/'result.json').exists())
        run(config);result=self.result(config)
        self.assertEqual(len(self.rows(result,42)),2)
        self.assertEqual(result['manifest']['tables'][0]['version'],1)
    def test_disk_and_retention_pressure_never_delete_old_files_or_publish(self):
        config=self.config_compact()
        with patch('incremental.storage.os.statvfs') as space:
            space.return_value.f_bavail=0;space.return_value.f_frsize=4096
            with self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):run(config)
        with patch('incremental.storage.MAX_RETAINED_BYTES',1),self.assertRaisesRegex(CaptureError,'incremental_retention_budget'):run(config)
        self.assertFalse((Path(config['workspace'])/'result.json').exists())
        self.assertEqual(len(self.rows(self.first,42)),1)
        run(config);self.assertEqual(len(self.rows(self.result(config),42)),2)
    def test_missing_old_root_and_changed_source_receipt_fail_closed(self):
        config=self.config_compact();run(config)
        config['previous']['epoch_id']=str(uuid.uuid4())
        with self.assertRaisesRegex(CaptureError,'compaction_source_changed'):run(config)
        config['generation']=str(self.root/'missing')
        config['previous_generation']=config['generation']
        with self.assertRaisesRegex(CaptureError,'incremental_history_lost'):run(config)
    def test_compaction_merges_fragmented_files_and_preserves_empty_table(self):
        from deltalake import DeltaTable,write_deltalake
        import pyarrow as pa
        import pyarrow.fs as fs
        from incremental.storage import inventory
        root=Path(self.config['generation'])
        for oid in (42,43):
            path=root/f'tables/{oid}'
            schema=DeltaTable(str(path)).to_pyarrow_dataset(filesystem=fs.SubTreeFileSystem(str(path),fs.LocalFileSystem())).schema
            if oid==42:
                for key in (10,20,30):write_deltalake(str(path),pa.Table.from_pylist([dict(id=key,amount=None,note='fragment')],schema=schema),mode='append')
            else:write_deltalake(str(path),pa.Table.from_pylist([],schema=schema),mode='overwrite')
            table=next(t for t in self.first['manifest']['tables'] if t['oid']==oid)
            table.update(version=DeltaTable(str(path)).version(),rows=4 if oid==42 else 0)
        self.first['manifest']['files']=inventory(root,self.first['manifest']['tables'])
        config=self.config_compact();run(config);result=self.result(config)
        metrics=result['manifest']['compaction']
        self.assertLess(metrics['output_files'],metrics['source_files'])
        self.assertEqual(len(self.rows(result,42)),5)
        self.assertEqual(self.rows(result,43),[])


if __name__=='__main__':unittest.main()
