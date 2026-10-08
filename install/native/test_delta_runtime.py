"""Reject substitution of upstream, stale, corrupt or wrong-target Delta artifacts."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from analytics import digest
from delta_runtime import ROOT, verify, verify_report


def sample_report(target):
    pin = json.loads((ROOT / 'components/deltalake-source.lock.json').read_text())
    suffix = 'linux_x86_64' if target == 'linux-x86_64' else 'macosx_15_0_arm64'
    return dict(schema_version=1, repository=pin['repository'], commit=pin['commit'],
                version=pin['version'], target=target, inputs=pin['inputs'], rustc='rustc ' + pin['rust'],
                maturin=pin['maturin'], patch=pin['patch'], cargo_lock=pin['cargo_lock'], profile=pin['profile'],
                source_lock_sha256=digest(ROOT / 'components/deltalake-source.lock.json'),
                builder_script_sha256=digest(ROOT / 'components/build-deltalake.py'),
                wheel=dict(file=f"deltalake-{pin['version']}-cp38-abi3-{suffix}.whl", sha256='a' * 64))


class DeltaArtifact(unittest.TestCase):
    def test_stale_source_patch_lock_toolchain_and_wrong_target_fail(self):
        original = sample_report('linux-x86_64')
        verify_report(original, 'linux-x86_64')
        for field, value in [('commit', 'b' * 40), ('patch', {}), ('cargo_lock', {}), ('target', 'macos-arm64'),
                             ('repository', 'https://example.org/delta-substitute'), ('rustc', 'rustc 1.0.0'),
                             ('profile', {}), ('builder_script_sha256', 'b' * 64)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_report({**original, field: value}, 'linux-x86_64')
        for file in ('deltalake-1.6.3-cp38-abi3-macosx_15_0_arm64.whl', '../escape.whl'):
            report = copy.deepcopy(original); report['wheel']['file'] = file
            with self.assertRaises(ValueError): verify_report(report, 'linux-x86_64')

    def test_inventory_and_wheel_bytes_are_bound_to_source_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Supply the locked source files without depending on a local Delta checkout.
            fixture_root = root / 'platform'; (fixture_root / 'components').mkdir(parents=True)
            for name in ('deltalake-source.lock.json', 'build-deltalake.py'):
                shutil.copy2(ROOT / 'components' / name, fixture_root / 'components' / name)
            artifact = root / 'artifact'; artifact.mkdir()
            report = sample_report('linux-x86_64')
            wheel = artifact / report['wheel']['file']
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('deltalake/_internal.abi3.so', b'synthetic extension')
                archive.writestr('deltalake-1.6.3.dist-info/METADATA', 'Name: deltalake\nVersion: 1.6.3\n')
            for name in ('Cargo.lock', 'LICENSE.txt', 'dependencies.json', 'bounded-merge.patch'):
                (artifact / name).write_text('synthetic fixture')
            pin_path = fixture_root / 'components/deltalake-source.lock.json'
            pin = json.loads(pin_path.read_text())
            pin['inputs']['LICENSE.txt'] = digest(artifact / 'LICENSE.txt')
            pin['cargo_lock']['sha256'] = digest(artifact / 'Cargo.lock')
            pin['patch']['sha256'] = digest(artifact / 'bounded-merge.patch')
            pin_path.write_text(json.dumps(pin))
            report.update(inputs=pin['inputs'], patch=pin['patch'], cargo_lock=pin['cargo_lock'], source_lock_sha256=digest(pin_path))
            report['wheel']['sha256'] = digest(wheel)
            report['files'] = {p.name: digest(p) for p in artifact.iterdir()}
            (artifact / 'deltalake-build.json').write_text(json.dumps(report))
            self.assertEqual(verify(artifact, 'linux-x86_64', root=fixture_root)[0], wheel)
            wheel.write_bytes(b'upstream replacement')
            with self.assertRaisesRegex(ValueError, 'checksum'): verify(artifact, 'linux-x86_64', root=fixture_root)
            wheel.unlink()
            with self.assertRaisesRegex(ValueError, 'inventory'): verify(artifact, 'linux-x86_64', root=fixture_root)


if __name__ == '__main__':
    unittest.main()
