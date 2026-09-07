#!/usr/bin/env python3
"""Bootstrap security/failure contracts against an actual loopback HTTP server."""
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import threading
import unittest

from stage import stage


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Installer(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sb-installer-')
        self.root = Path(self.temp.name).resolve()
        self.web = self.root / 'web'; self.web.mkdir()
        self.key = self.root / 'signing.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(self.key)], check=True, capture_output=True)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(self.web)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        target = 'macos-arm64' if platform.system() == 'Darwin' else 'linux-x86_64'
        self.version = 'v0.1.0-alpha.1'
        self.archive = self.web / f'supabricks-{self.version}-{target}.tar.gz'
        self.prefix = self.root / "programs ' spaced"
        self.env = dict(os.environ, SUPABRICKS_INSTALL_DIR=str(self.prefix), SUPABRICKS_NO_MODIFY_PATH='1')
        for name in list(self.env):
            if name.upper().endswith('_PROXY'):
                self.env.pop(name)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.temp.cleanup()

    def package(self, extra=None):
        # This tiny payload stands in only for the inventory verifier. Full
        # native qualification runs separately against the real release binary.
        with tarfile.open(self.archive, 'w:gz') as tar:
            for name in ['bin/supabricks', 'bin/psql']:
                data = b'#!/bin/bash\nexit 0\n'
                info = tarfile.TarInfo('supabricks/' + name); info.mode = 0o755; info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            if extra:
                tar.addfile(extra)
        checksum = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.archive.with_name(self.archive.name + '.sha256').write_text(f'{checksum}  {self.archive.name}\n')
        stage(self.web, self.version, self.base, self.key)

    def install(self, success):
        p = subprocess.run(['bash', str(self.web / 'install.sh')], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode == 0, success, p.stderr)
        return p

    def test_repeat_install_and_shell_quoting(self):
        self.package()
        self.install(True)
        original = os.readlink(self.prefix / 'current')
        self.install(True)
        self.assertEqual(os.readlink(self.prefix / 'current'), original)
        p = subprocess.run(['bash', '-c', '. "$SUPABRICKS_INSTALL_DIR/env"; command -v supabricks'], env=self.env, capture_output=True, text=True, check=True)
        self.assertEqual(p.stdout.strip(), str(self.prefix / 'bin/supabricks'))
        self.assertFalse((self.prefix / '.install-lock').exists())

    def test_tampered_signature_does_not_activate(self):
        self.package()
        self.archive.with_name(self.archive.name + '.sha256.sig').write_bytes(b'bad signature')
        self.assertIn('signature verification failed', self.install(False).stderr)
        self.assertFalse((self.prefix / 'current').exists())

    def test_changed_archive_does_not_activate(self):
        self.package()
        with self.archive.open('ab') as stream:
            stream.write(b'corrupted')
        self.assertIn('checksum verification failed', self.install(False).stderr)
        self.assertFalse((self.prefix / 'current').exists())

    def test_signed_path_traversal_rejected_before_extraction(self):
        self.package(tarfile.TarInfo('supabricks/../../escape'))
        self.assertIn('unsafe release archive paths', self.install(False).stderr)
        self.assertFalse((self.root / 'escape').exists())

    def test_signed_symlink_rejected_before_extraction(self):
        link = tarfile.TarInfo('supabricks/link'); link.type = tarfile.SYMTYPE; link.linkname = '/tmp'
        self.package(link)
        self.assertIn('links or special files', self.install(False).stderr)

    def test_concurrent_install_lock_keeps_current_release(self):
        self.package(); self.install(True)
        (self.prefix / '.install-lock').mkdir()
        self.assertIn('another installer owns', self.install(False).stderr)
        self.assertTrue((self.prefix / 'current/bin/supabricks').exists())

    def test_changed_release_bytes_rejected_on_repeat(self):
        self.package(); self.install(True)
        extra = tarfile.TarInfo('supabricks/new-file')
        self.package(extra)
        self.assertIn('different bytes', self.install(False).stderr)
        self.assertFalse((self.prefix / 'current/new-file').exists())

    def test_existing_user_binary_is_preserved(self):
        self.package()
        (self.prefix / 'bin').mkdir(parents=True)
        (self.prefix / 'bin/supabricks').write_text('user owned')
        self.assertIn('refusing to replace', self.install(False).stderr)
        self.assertEqual((self.prefix / 'bin/supabricks').read_text(), 'user owned')
        self.assertFalse((self.prefix / 'current').exists())


if __name__ == '__main__':
    unittest.main()
