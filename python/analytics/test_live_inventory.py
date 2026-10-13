"""A live Delta file rename must neither fence compaction nor hide disk usage."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from capture.spool import CaptureError
from incremental import storage


class LiveInventoryTests(unittest.TestCase):
    def test_rename_retries_complete_inventory_and_still_enforces_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);pending=root/'data.parquet#1';final=root/'data.parquet'
            pending.write_bytes(b'x'*128);(root/'other.parquet').write_bytes(b'x'*64)
            original=storage.files
            def discovered(path):
                result=original(path)
                if pending.exists():pending.rename(final)
                return result
            with patch.object(storage,'files',side_effect=discovered) as scan:
                self.assertEqual(storage.boundary(root,time.time()*1000+1000,live_writer=True),192)
                self.assertEqual(scan.call_count,2)
            with patch.object(storage,'MAX_BYTES',200),self.assertRaisesRegex(CaptureError,'incremental_disk_budget'):
                storage.boundary(root,time.time()*1000+1000,extra=9,live_writer=True)

    def test_repeated_disappearance_fails_closed_after_three_scans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch.object(storage,'files',return_value=[root/'vanished']) as scan:
                with self.assertRaisesRegex(CaptureError,'incremental_inventory_unstable'):
                    storage.boundary(root,time.time()*1000+1000,live_writer=True)
                self.assertEqual(scan.call_count,3)
            with patch.object(storage,'files',return_value=[root/'missing']) as scan:
                with self.assertRaises(FileNotFoundError):storage.boundary(root,time.time()*1000+1000)
                self.assertEqual(scan.call_count,1)
            descriptor=dict(manifest=dict(files=[dict(path='missing',bytes=128,sha256='a'*64)]))
            with self.assertRaises(FileNotFoundError):storage.verify_previous(root,descriptor)

    def test_retry_keeps_deadline_and_permission_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch.object(storage,'files',return_value=[root/'vanished']) as scan,patch.object(storage.time,'time',side_effect=[1,3]):
                with self.assertRaisesRegex(CaptureError,'apply_deadline'):storage.boundary(root,2000,live_writer=True)
                self.assertEqual(scan.call_count,1)
            with patch.object(storage,'files',side_effect=PermissionError):
                with self.assertRaises(PermissionError):storage.boundary(root,time.time()*1000+1000,live_writer=True)


if __name__=='__main__':unittest.main()
