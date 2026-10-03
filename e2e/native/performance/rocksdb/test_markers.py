import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from capture.spool import Spool
from rocks_journal import RocksJournal
import marker_profile
import marker_observer


class MarkerTests(unittest.TestCase):
    def test_only_successful_new_durable_records_are_observed(self):
        with tempfile.TemporaryDirectory() as temp:
            cell=Path(temp);(cell/'marker-observer').mkdir();(cell/'marker-observer/enabled').touch()
            class Instrumented(Spool):pass
            marker_profile.install({'Spool':Instrumented})
            root=cell/'capture'/'one'/'spool'
            with patch('capture.sqlite_journal.SQLiteJournal',RocksJournal):
                s=Instrumented(root,{'decoder_version':1});s.establish(100,{})
                payload=b'0'*21+struct.pack('!I',42)
                s.append(180,200,payload);s.append(180,200,payload)
                observer=SimpleNamespace(spool=root/'spool.sqlite3',capture_id='one',seq=0)
                self.assertEqual(marker_observer.rows(observer),[(1,'00000000000000c8',struct.pack('!I',42))])
                with self.assertRaises(Exception):s.append_many([(280,300,payload),(250,400,payload)])
                self.assertEqual(len(marker_observer.rows(observer)),1)
                observer.seq=1;self.assertEqual(marker_observer.rows(observer),[])
                s.close()
                s=Instrumented(root,{'decoder_version':1});s.append(280,300,payload);s.close()
                self.assertEqual(marker_observer.rows(observer),[(2,'000000000000012c',struct.pack('!I',42))])

    def test_partial_tail_waits_and_sequence_corruption_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'marker-observer').mkdir();path=root/'marker-observer/one.bin'
            record=marker_observer.RECORD.pack(1,200,42,1.)
            path.write_bytes(record[:12]);observer=SimpleNamespace(spool=root/'capture/one/spool/spool.sqlite3',capture_id='one',seq=0)
            self.assertEqual(marker_observer.rows(observer),[])
            path.write_bytes(record);self.assertEqual(len(marker_observer.rows(observer)),1)
            path.write_bytes(marker_observer.RECORD.pack(2,200,42,1.))
            with self.assertRaisesRegex(RuntimeError,'marker_sequence_gap'):marker_observer.rows(observer)
