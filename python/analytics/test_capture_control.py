import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import capture_worker as worker
from capture.journal import ReadBusy
from capture.spool import CaptureError


class CaptureControlTests(unittest.TestCase):
    def test_reuse_and_immediate_atomic_or_in_place_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'control.json';path.write_text('{"desired":"running"}')
            reader=worker.ControlReader(path)
            with patch.object(worker.json,'loads',wraps=json.loads) as parse:
                self.assertEqual(reader.read()['desired'],'running')
                for _ in range(100):self.assertEqual(reader.read()['desired'],'running')
                self.assertEqual(parse.call_count,1)
                replacement=path.with_suffix('.tmp');replacement.write_text('{"desired":"deleted"}')
                old=path.stat();os.utime(replacement,ns=(old.st_atime_ns,old.st_mtime_ns))
                replacement.replace(path)
                self.assertEqual(reader.read()['desired'],'deleted')
                self.assertEqual(parse.call_count,2)
                old=path.stat();path.write_text('{"desired":"running"}')
                os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
                self.assertEqual(reader.read()['desired'],'running')
                self.assertEqual(parse.call_count,3)

    def test_replacement_during_opened_read_never_caches_unlinked_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'control.json';path.write_text('{"generation":1}')
            replacement=path.with_suffix('.tmp');replacement.write_text('{"generation":2}')
            original=os.fstat;calls=[]
            def fstat(fd):
                meta=original(fd);calls.append(1)
                if len(calls)==1:replacement.replace(path)
                return meta
            reader=worker.ControlReader(path)
            with patch.object(worker.os,'fstat',side_effect=fstat):
                self.assertEqual(reader.read()['generation'],2)
            self.assertEqual(reader.read()['generation'],2)

    def test_failure_and_restart_discard_cached_control(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'control.json';path.write_text('{"generation":1}')
            reader=worker.ControlReader(path);reader.read();path.write_text('{')
            with self.assertRaises(ValueError):reader.read()
            self.assertIsNone(reader.signature);self.assertIsNone(reader.value)
            path.write_text('{"generation":2}')
            self.assertEqual(reader.read()['generation'],2)
            with patch.object(worker.json,'loads',wraps=json.loads) as parse:
                self.assertEqual(worker.ControlReader(path).read()['generation'],2)
                self.assertEqual(parse.call_count,1)
            path.unlink()
            with self.assertRaises(FileNotFoundError):reader.read()
            self.assertIsNone(reader.value)

    def test_unsafe_replacements_and_oversized_control_reject_cached_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'control.json';other=path.with_suffix('.other')
            path.write_text('{}');reader=worker.ControlReader(path);reader.read()
            path.rename(other);path.symlink_to(other)
            with self.assertRaises(CaptureError):reader.read()
            self.assertIsNone(reader.value)
            path.unlink();os.link(other,path)
            with self.assertRaises(CaptureError):reader.read()
            path.unlink();path.write_bytes(b' '*65537)
            with self.assertRaises(CaptureError):reader.read()
            self.assertIsNone(reader.value)

    def test_replacement_churn_is_bounded_and_does_not_require_resync(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'control.json';path.write_text('{"generation":1}')
            reader=worker.ControlReader(path);original=os.fstat;count=[]
            def fstat(fd):
                meta=original(fd);count.append(1)
                replacement=path.with_suffix('.tmp');replacement.write_text('{}');replacement.replace(path)
                return meta
            with patch.object(worker.os,'fstat',side_effect=fstat):
                with self.assertRaises(ReadBusy):reader.read()
            self.assertLessEqual(len(count),16)
            self.assertIsNone(reader.signature);self.assertIsNone(reader.value)


if __name__=='__main__':unittest.main()
