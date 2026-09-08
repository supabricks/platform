#!/usr/bin/env python3
"""Bootstrap security/failure contracts against an actual loopback HTTP server."""
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import platform
import signal
import subprocess
import tarfile
import tempfile
import threading
import unittest

from stage import stage


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == getattr(self.server, 'pause_path', None):
            self.send_response(200)
            self.send_header('Content-Length', '1000000')
            self.end_headers()
            self.wfile.write(b'x'); self.wfile.flush()
            self.server.paused.set()
            self.server.release.wait(timeout=15)
            return
        super().do_GET()

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


    def test_truncated_download_never_activates(self):
        self.package()
        content = self.archive.read_bytes()
        self.archive.write_bytes(content[:len(content)//2])
        self.assertIn('checksum verification failed', self.install(False).stderr)
        self.assertFalse((self.prefix / 'current').exists())
        self.assertFalse((self.prefix / '.install-lock').exists())

    def test_release_change_requires_upgrade_and_backup_options(self):
        self.package(); self.install(True)
        current = os.readlink(self.prefix / 'current')
        self.version = 'v0.1.0-alpha.2'
        self.archive = self.archive.with_name(self.archive.name.replace('alpha.1', 'alpha.2'))
        self.package()
        self.assertIn('SUPABRICKS_UPGRADE=1', self.install(False).stderr)
        self.env['SUPABRICKS_UPGRADE'] = '1'
        self.assertIn('SUPABRICKS_BACKUP_DIR', self.install(False).stderr)
        self.assertEqual(os.readlink(self.prefix / 'current'), current)

    def test_sigkill_during_upgrade_download_keeps_previous_release_and_data(self):
        self.package(); self.install(True)
        current = os.readlink(self.prefix / 'current')
        data = self.root / 'data'; data.mkdir(); (data / 'sentinel').write_text('retained')
        self.version = 'v0.1.0-alpha.2'
        self.archive = self.archive.with_name(self.archive.name.replace('alpha.1', 'alpha.2'))
        self.package()
        self.env.update(SUPABRICKS_UPGRADE='1', SUPABRICKS_BACKUP_DIR=str(self.root / 'backup'), SUPABRICKS_DATA_DIR=str(data))
        self.server.pause_path = '/' + self.archive.name
        self.server.paused = threading.Event(); self.server.release = threading.Event()
        child = subprocess.Popen(['bash', str(self.web / 'install.sh')], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            self.assertTrue(self.server.paused.wait(timeout=10), 'installer never reached payload download')
            os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)
        finally:
            self.server.release.set()
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL); child.wait(timeout=5)
        self.assertEqual(os.readlink(self.prefix / 'current'), current)
        self.assertEqual((data / 'sentinel').read_text(), 'retained')
        self.assertFalse((self.prefix / 'releases' / self.version).exists())
        # SIGKILL cannot run shell traps. Recovery is explicit and scoped to the
        # dead installer; never automatically remove another installer's lock.
        self.assertTrue((self.prefix / '.install-lock').exists())


if __name__ == '__main__':
    unittest.main()
