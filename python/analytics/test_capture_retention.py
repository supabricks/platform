import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

from capture.source import Source
from capture.spool import CaptureError,pg_lsn


class CaptureRetentionTests(unittest.TestCase):
    def source(self):
        source=Source.__new__(Source)
        source.config={'wal_bytes':512*1024**2};source.conn=Mock()
        source.snapshot_source=None;source.snapshot_at=None;source.snapshot_lsn=None;source.snapshot_requests=0
        source.conn.execute.return_value.fetchone.return_value=('0/10000000',)
        return source

    def test_snapshot_requests_are_pressure_byte_and_time_bounded(self):
        source=self.source();quantum=source.config['wal_bytes']//4
        slot=dict(source=pg_lsn(quantum),retained_bytes=quantum-1)
        with patch('capture.source.time.monotonic',return_value=1):source.maintain_restart(slot)
        source.conn.execute.assert_not_called()
        slot['retained_bytes']=quantum
        with patch('capture.source.time.monotonic',return_value=1):source.maintain_restart(slot)
        self.assertEqual(source.snapshot_requests,1)
        for _ in range(100):
            with patch('capture.source.time.monotonic',return_value=100):source.maintain_restart(slot)
        self.assertEqual(source.snapshot_requests,1,'idle pressure cannot generate a timer WAL backlog')
        slot['source']=pg_lsn(2*quantum)
        with patch('capture.source.time.monotonic',return_value=1.5):source.maintain_restart(slot)
        self.assertEqual(source.snapshot_requests,1)
        with patch('capture.source.time.monotonic',return_value=2):source.maintain_restart(slot)
        self.assertEqual(source.snapshot_requests,2)
        self.assertEqual([call.args[0] for call in source.conn.execute.call_args_list],
                         ['SELECT pg_log_standby_snapshot()::text']*2)

    def test_failed_snapshot_is_not_recorded_as_success(self):
        source=self.source();source.conn.execute.side_effect=OSError('unavailable')
        with self.assertRaises(OSError):source.maintain_restart(dict(source='0/10000000',retained_bytes=256*1024**2))
        self.assertIsNone(source.snapshot_source);self.assertIsNone(source.snapshot_at)
        self.assertEqual(source.snapshot_requests,0)

    def test_existing_retention_and_ack_guards_precede_maintenance(self):
        for code,confirmed,retained in [('wal_budget',100,512*1024**2*4//5),
                                        ('source_ack_ahead_of_spool',101,256*1024**2)]:
            with self.subTest(code=code):
                source=self.source();source.spool=SimpleNamespace(captured=100)
                source.conn.execute.return_value.fetchone.return_value=(512,)
                source.slot_status=Mock(return_value=dict(confirmed=pg_lsn(confirmed),retained_bytes=retained))
                source.maintain_restart=Mock()
                with self.assertRaises(CaptureError) as error:source.check()
                self.assertEqual(error.exception.code,code);source.maintain_restart.assert_not_called()


if __name__=='__main__':unittest.main()
