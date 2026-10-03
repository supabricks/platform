"""Physical accounting under concurrent native file retirement (#154)."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capture.spool import CaptureError
from rocks_journal import RocksJournal


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'rocksdb';self.path.mkdir(mode=0o700)
        self.journal=RocksJournal.__new__(RocksJournal);self.journal.path=self.path
        self.file=self.path/'000001.sst';self.file.write_bytes(b'x'*4096)

    @contextmanager
    def retire_during_stat(self):
        # Return actual kernel metadata from the inode looked up before unlink.
        # The open descriptor makes that timing deterministic without editing
        # native files in a running database or inventing stat fields.
        original=Path.lstat
        with self.file.open('rb') as stream:
            def retired(path,*args,**kwargs):
                if path==self.file:
                    path.unlink()
                    meta=os.fstat(stream.fileno())
                    self.assertEqual(meta.st_nlink,0)
                    return meta
                return original(path,*args,**kwargs)
            with patch.object(Path,'lstat',retired):yield

    def test_retired_regular_file_is_counted_in_current_sample(self):
        with self.retire_during_stat():
            self.assertEqual(self.journal.sizes(),{'000001.sst':4096})
        self.assertEqual(self.journal.sizes(),{})

    def test_retired_file_still_enforces_physical_budget(self):
        self.journal.limit=4095
        with self.retire_during_stat(),self.assertRaisesRegex(CaptureError,'spool_budget'):
            self.journal.after_write()

    def test_file_gone_before_lookup_is_already_handled(self):
        original=Path.lstat
        def disappeared(path,*args,**kwargs):
            if path==self.file:path.unlink()
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',disappeared):self.assertEqual(self.journal.sizes(),{})

    def test_real_hardlink_is_still_rejected(self):
        os.link(self.file,self.path/'duplicate')
        with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):self.journal.sizes()

    def test_symlink_and_directory_are_still_rejected(self):
        self.file.unlink();self.file.symlink_to(self.path)
        with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):self.journal.sizes()
        self.file.unlink();self.file.mkdir()
        with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):self.journal.sizes()

    def test_foreign_ownership_is_rejected_even_after_retirement(self):
        uid=os.getuid()
        with self.retire_during_stat(),patch('rocks_journal.os.getuid',return_value=uid+1):
            with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):self.journal.sizes()

    def test_lookup_permission_errors_are_not_hidden(self):
        original=Path.lstat
        def denied(path,*args,**kwargs):
            if path==self.file:raise PermissionError('injected lookup denial')
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',denied),self.assertRaises(PermissionError):self.journal.sizes()


if __name__=='__main__':unittest.main()
