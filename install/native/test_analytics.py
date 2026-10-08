#!/usr/bin/env python3
"""Boundary regressions for native analytical packaging."""
import hashlib
import http.client
import io
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from analytics import fetch, macho_dependencies, worker_launcher


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'pinned.whl'
        self.data = b'verified wheel bytes'
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.sleep = patch('analytics.time.sleep').start()
        self.addCleanup(patch.stopall)

    def test_valid_cache_never_uses_network_and_corrupt_cache_fails_closed(self):
        with patch('analytics.urllib.request.urlopen') as open_url:
            self.path.write_bytes(self.data)
            fetch('https://example.invalid/pinned.whl', self.sha, self.path)
            self.path.write_bytes(b'corrupt')
            with self.assertRaises(ValueError):
                fetch('https://example.invalid/pinned.whl', self.sha, self.path)
            open_url.assert_not_called()

    def test_connection_timeout_retries_then_publishes_only_verified_bytes(self):
        with patch('analytics.urllib.request.urlopen', side_effect=[
                urllib.error.URLError(TimeoutError()), io.BytesIO(self.data)]) as open_url:
            fetch('https://example.invalid/pinned.whl', self.sha, self.path)
        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_interrupted_body_leaves_no_final_or_partial_cache_entry(self):
        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise http.client.IncompleteRead(b'partial')
                return super().read(size)
        def opened(*args, **kwargs):
            self.assertFalse(self.path.exists())
            return Interrupted(b'partial')
        with patch('analytics.urllib.request.urlopen', side_effect=opened) as open_url:
            with self.assertRaises(http.client.IncompleteRead):
                fetch('https://example.invalid/pinned.whl', self.sha, self.path)
        self.assertEqual(open_url.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_checksum_mismatch_is_not_retried_or_cached(self):
        with patch('analytics.urllib.request.urlopen', return_value=io.BytesIO(b'wrong')) as open_url:
            with self.assertRaises(ValueError):
                fetch('https://example.invalid/pinned.whl', self.sha, self.path)
        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_certificate_failure_is_not_retried(self):
        with patch('analytics.urllib.request.urlopen', side_effect=
                urllib.error.URLError(ssl.SSLCertVerificationError('invalid certificate'))) as open_url:
            with self.assertRaises(urllib.error.URLError):
                fetch('https://example.invalid/pinned.whl', self.sha, self.path)
        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_only_retryable_http_statuses_are_retried(self):
        for code, calls in [(404, 1), (429, 3), (503, 3)]:
            with self.subTest(code=code), patch('analytics.urllib.request.urlopen',
                    side_effect=urllib.error.HTTPError('https://example.invalid', code, 'failure', {}, None)) as open_url:
                with self.assertRaises(urllib.error.HTTPError):
                    fetch('https://example.invalid/pinned.whl', self.sha, self.path)
                self.assertEqual(open_url.call_count, calls)
                self.assertEqual(list(self.path.parent.iterdir()), [])


class WorkerLauncherTests(unittest.TestCase):
    def test_linux_memory_policy_overrides_inherited_huge_page_setting(self):
        with tempfile.TemporaryDirectory(prefix='runtime with spaces ') as directory:
            root = Path(directory)
            (root/'analytics').mkdir()
            (root/'runtime/bin').mkdir(parents=True)
            python = root/'runtime/bin/python3.12'
            python.write_text('#!/bin/bash\nprintf "%s\\n" "${_RJEM_MALLOC_CONF-unset}" "$@"\n')
            python.chmod(0o755)
            launcher = root/'analytics/python'
            for target, expected in [('linux-x86_64', 'thp:never'), ('macos-arm64', 'thp:always')]:
                launcher.write_text(worker_launcher(target));launcher.chmod(0o755)
                out = subprocess.check_output([str(launcher), 'script with spaces.py'], text=True,
                    env={**os.environ, '_RJEM_MALLOC_CONF':'thp:always'})
                self.assertEqual(out.splitlines(), [expected, '-E', '-s', '-B', 'script with spaces.py'])


class MachOLoads(unittest.TestCase):
    def test_library_identity_and_universal_headers_are_not_dependencies(self):
        # psycopg wheels retain an absolute /DLC identity after their actual
        # load commands have been repaired to use adjacent bundled libraries.
        listing = '''/private/release/lib.so (architecture arm64):
Load command 0
          cmd LC_ID_DYLIB
      cmdsize 80
         name /DLC/psycopg_binary/.dylibs/libcom_err.3.0.dylib (offset 24)
Load command 1
          cmd LC_LOAD_DYLIB
         name @loader_path/libkrb5.dylib (offset 24)
Load command 2
          cmd LC_RPATH
         path @loader_path/../lib (offset 12)
/private/release/lib.so (architecture x86_64):
Load command 0
          cmd LC_LOAD_WEAK_DYLIB
         name /opt/homebrew/lib/unbundled.dylib (offset 24)
'''
        self.assertEqual(list(macho_dependencies(listing)), [
            '@loader_path/libkrb5.dylib', '@loader_path/../lib', '/opt/homebrew/lib/unbundled.dylib'])


if __name__ == '__main__':
    unittest.main()
