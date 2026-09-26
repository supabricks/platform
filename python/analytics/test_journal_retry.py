"""SP01 read snapshots, bounded contention, and non-retryable integrity errors."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from capture.spool import Spool, CaptureError
from incremental import storage


def sql_error(code):
    error=sqlite3.OperationalError('injected sqlite failure')
    error.sqlite_errorcode=code
    return error


class JournalRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.spool=Spool(Path(self.temp.name)/'spool?literal',{'generation':'one','decoder_version':1})
        self.spool.establish(100,{})
        self.spool.set('bootstrap',{'lsn':'0/64'})
        self.spool.append(280,300,b'complete')
        self.config=dict(spool=str(self.spool.path),identity=self.spool.get('identity'),bootstrap_lsn='0/64',after_lsn='0/64',target_lsn='0/12C',deadline_ms=int(time.time()*1000)+60000)
    def tearDown(self):
        self.spool.close();self.temp.cleanup()
    def test_short_writer_lock_reopens_read_snapshot(self):
        with sqlite3.connect(self.spool.path) as db:
            db.execute('BEGIN EXCLUSIVE')
            def release(seconds):
                # Release only after SQLite reported contention. A timed helper
                # thread can release before a descheduled CI reader ever runs.
                db.rollback();time.sleep(seconds)
            with patch.object(storage,'journal_backoff',release):result=storage.journal(self.config)
        self.assertEqual(result,({},[(300,b'complete')],300,8))
        self.assertGreater(self.config['_journal_read']['busy'],0)
        self.assertGreater(self.config['_journal_read']['attempts'],1)
        self.assertEqual(self.config['_journal_read']['outcome'],'complete')
        self.spool.append(380,400,b'next')
    def test_persistent_lock_has_one_three_second_budget(self):
        with sqlite3.connect(self.spool.path) as db:
            db.execute('BEGIN EXCLUSIVE');started=time.monotonic()
            with self.assertRaises(storage.JournalBusyDeferred):storage.journal(self.config)
            elapsed=time.monotonic()-started
        # Shared CI scheduling may delay return; the virtual-clock test below
        # checks the exact requested budget without relying on wall-clock jitter.
        self.assertGreater(elapsed,2.9);self.assertLess(elapsed,5)
        self.assertLessEqual(self.config['_journal_read']['attempts'],32)
        self.assertEqual(self.config['_journal_read']['outcome'],'deferred')
        self.assertEqual(storage.journal(self.config)[2],300)
    def test_global_deadline_caps_sqlite_and_backoff(self):
        with sqlite3.connect(self.spool.path) as db:
            db.execute('BEGIN EXCLUSIVE')
            self.config['deadline_ms']=int(time.time()*1000)+180
            started=time.monotonic()
            with self.assertRaises(storage.JournalBusyDeferred):storage.journal(self.config)
            self.assertLess(time.monotonic()-started,1)
    def test_retry_and_backoff_never_reset_the_monotonic_budget(self):
        for budget in (.18,3):
            with self.subTest(budget=budget):
                clock=[100.0];deadlines=[]
                self.config['deadline_ms']=100000+budget*1000
                def attempt(request,deadline):
                    deadlines.append(deadline)
                    clock[0]+=min(.1,max(0,deadline-clock[0]))
                    raise sql_error(sqlite3.SQLITE_BUSY)
                def backoff(delay):clock[0]+=delay
                with patch.object(storage.time,'monotonic',side_effect=lambda:clock[0]),patch.object(storage.time,'time',return_value=100),patch.object(storage,'journal_attempt',attempt),patch.object(storage,'journal_backoff',backoff),self.assertRaises(storage.JournalBusyDeferred):
                    storage.journal(self.config)
                self.assertEqual(set(deadlines),{100+budget})
                self.assertAlmostEqual(clock[0],100+budget)
                self.assertLessEqual(len(deadlines),32)
    def test_deadline_crossed_inside_a_retry_remains_a_safe_deferral(self):
        with patch.object(storage,'journal_attempt',side_effect=[sql_error(sqlite3.SQLITE_BUSY),CaptureError('journal_read_deadline')]),self.assertRaises(storage.JournalBusyDeferred):
            storage.journal(self.config)
        self.assertEqual(self.config['_journal_read']['outcome'],'deferred')
        self.assertEqual(self.config['_journal_read']['busy'],1)
        # Without a recognized BUSY error, a slow read is not retried as contention.
        with patch.object(storage,'journal_attempt',side_effect=CaptureError('journal_read_deadline')) as attempt,self.assertRaisesRegex(CaptureError,'journal_read_deadline'):
            storage.journal(self.config)
        self.assertEqual(attempt.call_count,1)
    def test_only_recognized_busy_codes_are_retried(self):
        for code in (sqlite3.SQLITE_BUSY,261,517,773,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_IOERR,sqlite3.SQLITE_CORRUPT):
            with self.subTest(code=code),patch.object(storage,'journal_attempt',side_effect=[sql_error(code),('ok',)]) as attempt:
                if code in storage.BUSY_CODES:
                    self.assertEqual(storage.journal(self.config),('ok',));self.assertEqual(attempt.call_count,2)
                else:
                    with self.assertRaises(sqlite3.OperationalError):storage.journal(self.config)
                    self.assertEqual(attempt.call_count,1)
    def test_retry_request_is_immutable(self):
        original=copy.deepcopy(self.config);seen=[]
        def attempt(request,deadline):
            seen.append(copy.deepcopy(request))
            if len(seen)==1:raise sql_error(sqlite3.SQLITE_BUSY)
            return ('ok',)
        def mutate(_):
            self.config['target_lsn']='0/999';self.config['identity']['generation']='other'
        with patch.object(storage,'journal_attempt',attempt),patch.object(storage,'journal_backoff',mutate):storage.journal(self.config)
        self.assertEqual(seen[0],seen[1]);self.assertEqual(seen[1]['identity'],original['identity'])
    def test_payload_failure_discards_partial_rows_and_closes_snapshot(self):
        self.spool.append(380,400,b'second');self.config['target_lsn']='0/190'
        connect=sqlite3.connect;opened=[]
        class Connection:
            def __init__(self,*a,**k):self.db=connect(*a,**k);self.closed=False;opened.append(self)
            def set_progress_handler(self,*a):self.db.set_progress_handler(*a)
            def execute(self,sql,*a):
                cursor=self.db.execute(sql,*a)
                if sql.startswith('SELECT end_lsn') and len(opened)==1:
                    class Rows:
                        count=0
                        def __iter__(self):return self
                        def __next__(self):
                            self.count+=1
                            if self.count==2:raise sql_error(sqlite3.SQLITE_BUSY)
                            return next(cursor)
                        def close(self):cursor.close()
                    return Rows()
                return cursor
            def close(self):self.db.close();self.closed=True
        def backoff(seconds):
            # Even while the caught exception retains its traceback/cursor,
            # the failed snapshot must release every reader lease before sleep.
            with connect(self.spool.path,timeout=0) as writer:
                writer.execute('BEGIN EXCLUSIVE')
            time.sleep(seconds)
        with patch.object(storage.sqlite3,'connect',Connection),patch.object(storage,'journal_backoff',backoff):result=storage.journal(self.config)
        self.assertEqual(result[1],[(300,b'complete'),(400,b'second')])
        self.assertEqual(len(opened),2);self.assertTrue(all(db.closed for db in opened))
    def test_pruning_between_attempts_fails_history_validation(self):
        self.spool.append(380,400,b'x'*(1024*1024));self.spool.append(480,500,b'anchor')
        self.config['target_lsn']='0/1F4';attempt=storage.journal_attempt;count=0
        def first_busy(request,deadline):
            nonlocal count
            count+=1
            if count==1:raise sql_error(sqlite3.SQLITE_BUSY)
            return attempt(request,deadline)
        def prune(_):self.assertGreater(self.spool.prune('0/1F4'),0)
        with patch.object(storage,'journal_attempt',first_busy),patch.object(storage,'journal_backoff',prune),self.assertRaisesRegex(CaptureError,'source_history_lost'):
            storage.journal(self.config)
        self.assertEqual(count,2)
    def test_corruption_and_identity_errors_are_not_retried(self):
        for code in ('spool_corrupt','spool_identity_mismatch','source_history_lost','spool_history_gap'):
            with self.subTest(code=code),patch.object(storage,'journal_attempt',side_effect=CaptureError(code)) as attempt,self.assertRaisesRegex(CaptureError,code):storage.journal(self.config)
            self.assertEqual(attempt.call_count,1)

if __name__=='__main__':unittest.main()
