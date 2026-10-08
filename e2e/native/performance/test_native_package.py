import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from native_package import overlay, sha


class NativePackageTests(unittest.TestCase):
    def fixture(self, root):
        base = root/'base'
        (base/'bin').mkdir(parents=True)
        (base/'bin/supabricks').write_bytes(b'old native binary')
        (base/'bin/supabricks').chmod(0o755)
        (base/'dependency').write_bytes(b'shared dependency')
        manifest = dict(format_version=1, version='test', target='linux-x86_64',
                        profile='test', provenance={'source': 'unchanged'},
                        files={name: dict(sha256=sha(base/name), executable=name.startswith('bin/'))
                               for name in ('bin/supabricks', 'dependency')})
        (base/'release.json').write_text(json.dumps(manifest))
        binary = root/'new-binary'
        binary.write_bytes(b'new native binary')
        return base, binary, manifest

    def test_strict_schema_and_external_proof_preserve_shared_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, binary, manifest = self.fixture(root)
            identity = sha(base/'release.json')
            destination, proof = root/'overlay', root/'proof.json'

            def verify(command, **kwargs):
                self.assertEqual(command, [str(destination/'bin/supabricks'), 'installation', 'verify'])
                generated = json.loads((destination/'release.json').read_text())
                self.assertEqual(set(generated), set(manifest), 'native manifest rejects new fields')
                self.assertEqual(generated['provenance'], manifest['provenance'])
                return json.dumps(dict(verified=True, identity=sha(destination/'release.json')))

            with patch('native_package.subprocess.check_output', side_effect=verify):
                overlay(base, binary, destination, 'a'*40, proof)
            self.assertEqual(sha(base/'release.json'), identity)
            self.assertEqual((base/'bin/supabricks').read_bytes(), b'old native binary')
            self.assertEqual((destination/'bin/supabricks').read_bytes(), b'new native binary')
            self.assertEqual((base/'dependency').stat().st_ino, (destination/'dependency').stat().st_ino)
            self.assertEqual(json.loads(proof.read_text())['native_revision'], 'a'*40)

    def test_failed_runtime_verification_never_emits_success_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, binary, _ = self.fixture(root)
            proof = root/'proof.json'
            with patch('native_package.subprocess.check_output',
                       side_effect=subprocess.CalledProcessError(2, 'installation verify')):
                with self.assertRaises(subprocess.CalledProcessError):
                    overlay(base, binary, root/'overlay', 'a'*40, proof)
            self.assertFalse(proof.exists())

    def test_catalog_upgrade_is_declared_without_mutating_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, binary, manifest = self.fixture(root)
            manifest['provenance']['data_formats'] = dict(local_catalog=29)
            (base/'release.json').write_text(json.dumps(manifest))
            identity = sha(base/'release.json')
            destination, proof = root/'overlay', root/'proof.json'
            with patch('native_package.subprocess.check_output', side_effect=lambda *a, **kw:
                       json.dumps(dict(verified=True, identity=sha(destination/'release.json')))):
                overlay(base, binary, destination, 'a'*40, proof, local_catalog=30)
            self.assertEqual(sha(base/'release.json'), identity)
            self.assertEqual(json.loads((destination/'release.json').read_text())['provenance']['data_formats']['local_catalog'], 30)
            self.assertEqual(json.loads(proof.read_text())['local_catalog'], dict(before=29, after=30))


if __name__ == '__main__':
    unittest.main()
