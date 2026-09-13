"""The completion gate must reject stale or incomplete qualification evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from environment_evidence import SUITES, collect


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for target in ('linux-x86_64', 'macos-arm64'):
            for suite, files in SUITES.items():
                for name, minimum in files.items():
                    path = self.root / f'{suite}-{target}' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    value = dict(status='passed', release_sha256=target, checks=list(range(minimum)))
                    if suite == 'release-environment-lifecycle' and name == 'qualification.json':
                        value.update(target=target, source=dict(platform_commit='final', platform_dirty=False),
                            archives=dict(new=dict(target=target), old={}), release_identity=target,
                            python_version='3.12.13', kernel_contract_sha256='contract',
                            wheels={}, notices={}, measurements={})
                    path.write_text(json.dumps(value))

    def change(self, suite, name, **fields):
        path = self.root / (suite + '-linux-x86_64') / name
        value = json.loads(path.read_text()); value.update(fields); path.write_text(json.dumps(value))

    def test_complete_matching_evidence(self):
        self.assertEqual(collect(self.root, 'final')['status'], 'passed')

    def test_stale_report_cannot_complete_new_release(self):
        self.change('release-environment-console', 'environments.json', release_sha256='old')
        with self.assertRaisesRegex(AssertionError, 'mixed release'):
            collect(self.root, 'final')

    def test_missing_or_short_suite_cannot_pass(self):
        self.change('release-environments', 'qualification-kernels.json', checks=[])
        with self.assertRaisesRegex(AssertionError, 'incomplete'):
            collect(self.root, 'final')
        (self.root / 'release-environments-linux-x86_64/qualification-kernels.json').unlink()
        with self.assertRaises(FileNotFoundError):
            collect(self.root, 'final')

    def test_failure_and_cleanup_are_not_success(self):
        for fields in (dict(status='failed'), dict(status='passed', cleanup_failed=True)):
            self.change('release-packages', 'qualification.json', **fields)
            with self.assertRaises(AssertionError): collect(self.root, 'final')

    def test_source_build_or_different_revision_cannot_pass(self):
        for source in (dict(platform_commit='old', platform_dirty=False), dict(platform_commit='final', platform_dirty=True)):
            self.change('release-environment-lifecycle', 'qualification.json', source=source)
            with self.assertRaises(AssertionError): collect(self.root, 'final')


if __name__ == '__main__':
    unittest.main()
