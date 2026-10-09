import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import contextmanager
from source_profile import Ledger, TimedConnection
from analyze_source_profile import reconcile


class FakeDB:
    info = SimpleNamespace(backend_pid=123)
    def __init__(self, fail_commit=False):
        self.events = []; self.fail_commit = fail_commit
    @contextmanager
    def transaction(self):
        self.events.append('BEGIN')
        try:
            yield
        except BaseException:
            self.events.append('ROLLBACK'); raise
        else:
            self.events.append('COMMIT')
            if self.fail_commit:
                raise TimeoutError('ambiguous commit')
    def cursor(self):
        return self
    @contextmanager
    def copy(self, sql):
        self.events.append('COPY'); yield self; self.events.append('COPY_DONE')
    def write(self, data):
        self.events.append(('WRITE', data))
    def execute(self, sql):
        self.events.append(sql)
        return SimpleNamespace(fetchone=lambda: ('0/10',))


class SourceProfileTests(unittest.TestCase):
    def test_join_rejects_missing_ack_and_wrong_copy_bytes(self):
        event = dict(success=True, ordinal=1, rows=1, encoded_bytes=2,
                     committed_rows=1, start_ns=1, end_ns=20,
                     **{k+'_ns':1 for k in ('begin','copy_start','copy_write','copy_finish','lsn_query','commit')})
        attempt=dict(kind='attempt',rows=1,encoded_bytes=2,table='t',end_offset=2,copy_sha256='abc')
        ack=dict(kind='ack',rows=1,table='t',end_offset=2,copy_and_commit_ms=1)
        self.assertEqual(reconcile([event], [attempt,ack]),1)
        with self.assertRaises(AssertionError):
            reconcile([event], [attempt])
        with self.assertRaises(AssertionError):
            reconcile([event], [dict(attempt,encoded_bytes=3),ack])

    def test_success_preserves_transaction_and_copy_and_no_values(self):
        with tempfile.TemporaryDirectory() as root:
            sink = Ledger(Path(root) / 'events'); raw = FakeDB(); db = TimedConnection(raw, sink)
            with db.transaction():
                with db.cursor().copy('COPY t FROM STDIN') as copy:
                    copy.write(b'private-row\n')
                self.assertEqual(db.execute('SELECT pg_current_wal_insert_lsn()::text').fetchone(), ('0/10',))
            self.assertEqual(raw.events, ['BEGIN','COPY',('WRITE',b'private-row\n'),'COPY_DONE','SELECT pg_current_wal_insert_lsn()::text','COMMIT'])
            sink.close(); text = (Path(root) / 'events').read_text(); row = json.loads(text)
            self.assertNotIn('private-row', text); self.assertNotIn('COPY t', text)
            self.assertEqual((row['rows'],row['committed_rows'],row['success']), (1,1,True))
            self.assertTrue(all(row[k + '_ns'] > 0 for k in ('begin','copy_start','copy_write','copy_finish','lsn_query','commit')))
    def test_copy_failure_rolls_back_without_ack_or_retry(self):
        with tempfile.TemporaryDirectory() as root:
            sink=Ledger(Path(root)/'events'); raw=FakeDB(); db=TimedConnection(raw,sink)
            with self.assertRaisesRegex(ValueError, 'source failure'):
                with db.transaction():
                    with db.cursor().copy('COPY t FROM STDIN'):
                        raise ValueError('source failure')
            sink.close(); row=json.loads((Path(root)/'events').read_text())
            self.assertEqual(raw.events,['BEGIN','COPY','ROLLBACK'])
            self.assertFalse(row['success']); self.assertEqual(row['committed_rows'],0)
    def test_ambiguous_commit_and_full_ledger_never_replay_source(self):
        with tempfile.TemporaryDirectory() as root:
            sink=Ledger(Path(root)/'events',limit=1); raw=FakeDB(True); db=TimedConnection(raw,sink)
            with self.assertRaises(TimeoutError):
                with db.transaction():
                    pass
            self.assertEqual(raw.events,['BEGIN','COMMIT']); self.assertEqual(db.rows,0)
            self.assertEqual(sink.close()['dropped'],1)


if __name__ == '__main__':
    unittest.main()
