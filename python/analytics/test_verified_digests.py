"""Content-cache invalidation and bounded lifetime, including restored timestamps."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from capture.spool import CaptureError
from incremental import storage
from incremental.planning import mutation_lease


class VerifiedDigests(unittest.TestCase):
    def setUp(self):
        self.mask=os.umask(0o077)
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.path=self.root/'data';self.path.write_bytes(b'original')
        self.config=dict(identity=dict(generation='one'),worker_generation=1,source_revision=1)
        storage._hashes.clear();storage._hash_scope=None

    def tearDown(self):
        self.tmp.cleanup();os.umask(self.mask)
        storage._hashes.clear();storage._hash_scope=None

    def test_only_leased_same_scope_reuses_bytes_and_restart_rehashes(self):
        original=hashlib.sha256;hashers=[]
        def hasher():
            result=Mock(wraps=original());hashers.append(result);return result
        with patch('incremental.storage.hashlib.sha256',side_effect=hasher):
            with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
                first=storage.digest(self.path)
                self.assertEqual(storage.digest(self.path),first)
                self.assertEqual(len(storage._hashes),1)
            # A repeated request may reuse, but an unleased caller cannot.
            storage.digest(self.path)
            with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
                self.assertEqual(storage.digest(self.path),first)
            # Simulate a new process: no content evidence survives restart.
            storage._hashes.clear()
            with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
                self.assertEqual(storage.digest(self.path),first)
        self.assertEqual(sum(h.update.call_count for h in hashers),3)

    def test_same_length_write_with_restored_mtime_cannot_reuse(self):
        with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
            first=storage.digest(self.path);before=self.path.stat()
            self.path.write_bytes(b'modified')
            os.utime(self.path,ns=(before.st_atime_ns,before.st_mtime_ns))
            self.assertNotEqual(storage.digest(self.path),first)

    def test_replacement_symlink_permissions_and_expected_hash_mismatch(self):
        with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
            first=storage.digest(self.path)
            with self.assertRaisesRegex(CaptureError,'incremental_checksum'):
                storage.verify_previous(self.root,dict(manifest=dict(files=[
                    dict(path='data',bytes=8,sha256='0'*64)])))
            replacement=self.root/'replacement';replacement.write_bytes(b'replaced');replacement.replace(self.path)
            self.assertNotEqual(storage.digest(self.path),first)
            self.path.chmod(0o666)
            with self.assertRaises(CaptureError):storage.digest(self.path)
            self.path.unlink();self.path.symlink_to('/dev/null')
            with self.assertRaises(OSError):storage.digest(self.path)

    def test_scope_failure_and_lost_lease_invalidate_evidence(self):
        with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
            storage.digest(self.path)
        with mutation_lease(self.root) as lease,storage.verified_digests(lease,dict(self.config,worker_generation=2)):
            self.assertFalse(storage._hashes)
            storage.digest(self.path)
        with self.assertRaisesRegex(ValueError,'abort'):
            with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
                storage.digest(self.path)
                raise ValueError('abort')
        self.assertFalse(storage._hashes)
        with self.assertRaises(CaptureError):
            with mutation_lease(self.root) as lease:
                with storage.verified_digests(lease,self.config):
                    storage.digest(self.path);lease.fd=None
        self.assertFalse(storage._hashes)

    def test_bound_and_parent_symlink_rejection(self):
        with patch.object(storage,'MAX_FILES',2):
            with mutation_lease(self.root) as lease,storage.verified_digests(lease,self.config):
                for i in range(3):
                    path=self.root/str(i);path.write_bytes(bytes([i]));storage.digest(path)
                self.assertEqual(len(storage._hashes),2)
                nested=self.root/'nested';nested.symlink_to(self.root,target_is_directory=True)
                with self.assertRaises(CaptureError):storage.digest(nested/'data')


if __name__=='__main__':unittest.main()
