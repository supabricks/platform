"""Reject substitution of upstream, stale, corrupt or wrong-target Sail artifacts."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from analytics import digest
from sail import ROOT, verify, verify_report


def sample_report(target):
    pin = json.loads((ROOT / 'components/sail-source.lock.json').read_text())
    suffix = 'linux_x86_64' if target == 'linux-x86_64' else 'macosx_15_0_arm64'
    return dict(schema_version=1, repository=pin['repository'], commit=pin['commit'], source_dirty=False,
                version=pin['version'], target=target, inputs=pin['inputs'], rustc='rustc ' + pin['rust'],
                maturin=pin['maturin'], protoc=pin['targets'][target]['protoc'], profile=pin['profile'],
                source_lock_sha256=digest(ROOT / 'components/sail-source.lock.json'),
                builder_script_sha256=digest(ROOT / 'components/build-sail.py'),
                wheel=dict(file=f"pysail-{pin['version']}-cp38-abi3-{suffix}.whl", sha256='a' * 64))


class SailArtifact(unittest.TestCase):
    def test_stale_source_dirty_tree_toolchain_and_wrong_target_fail(self):
        original = sample_report('linux-x86_64')
        verify_report(original, 'linux-x86_64')
        for field, value in [('commit', 'b' * 40), ('source_dirty', True), ('target', 'macos-arm64'),
                             ('repository', 'https://github.com/lakehq/sail'), ('rustc', 'rustc 1.0.0'),
                             ('profile', {}), ('builder_script_sha256', 'b' * 64)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_report({**original, field: value}, 'linux-x86_64')
        for file in ('pysail-0.7.1-cp38-abi3-macosx_15_0_arm64.whl', '../escape.whl'):
            report = copy.deepcopy(original); report['wheel']['file'] = file
            with self.assertRaises(ValueError): verify_report(report, 'linux-x86_64')

    def test_inventory_and_wheel_bytes_are_bound_to_source_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Supply the locked source files without depending on a local Sail checkout.
            fixture_root = root / 'platform'; (fixture_root / 'components').mkdir(parents=True)
            for name in ('sail-source.lock.json', 'build-sail.py'):
                shutil.copy2(ROOT / 'components' / name, fixture_root / 'components' / name)
            artifact = root / 'artifact'; artifact.mkdir()
            report = sample_report('linux-x86_64')
            wheel = artifact / report['wheel']['file']
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('pysail/_native.abi3.so', b'synthetic extension')
                archive.writestr('pysail-0.7.1.dist-info/METADATA', 'Name: pysail\nVersion: 0.7.1\n')
            for name in ('Cargo.lock', 'LICENSE', 'dependencies.json'):
                (artifact / name).write_text('synthetic fixture')
            pin_path = fixture_root / 'components/sail-source.lock.json'
            pin = json.loads(pin_path.read_text())
            for name in ('Cargo.lock', 'LICENSE'): pin['inputs'][name] = digest(artifact / name)
            pin_path.write_text(json.dumps(pin))
            report.update(inputs=pin['inputs'], source_lock_sha256=digest(pin_path))
            report['wheel']['sha256'] = digest(wheel)
            report['files'] = {p.name: digest(p) for p in artifact.iterdir()}
            (artifact / 'sail-build.json').write_text(json.dumps(report))
            self.assertEqual(verify(artifact, 'linux-x86_64', root=fixture_root)[0], wheel)
            wheel.write_bytes(b'upstream replacement')
            with self.assertRaisesRegex(ValueError, 'checksum'): verify(artifact, 'linux-x86_64', root=fixture_root)
            wheel.unlink()
            with self.assertRaisesRegex(ValueError, 'inventory'): verify(artifact, 'linux-x86_64', root=fixture_root)


if __name__ == '__main__':
    unittest.main()
