import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from platform_candidate import WORKER, replacement_files
from sail_candidate import digest, validate


class PlatformCandidateChecks(unittest.TestCase):
    def test_worker_must_match_committed_source_and_binary_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, data in [('bin/supabricks', b'binary'), (WORKER, b'worker')]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            report = dict(commit='a' * 40, source_dirty=False,
                          command=['cargo', 'build', '--release', '--locked', '-p', 'supabricks-local'],
                          cargo_lock_sha256=digest(b'lock'),
                          files={'bin/supabricks': digest(b'binary'), WORKER: digest(b'worker')})
            def save(value):
                (root / 'platform-build.json').write_text(json.dumps(value))
            def source(command, **kwargs):
                return b'worker' if command[-1].endswith(WORKER) else b'lock'
            with patch('platform_candidate.subprocess.check_output', side_effect=source):
                save(report)
                self.assertEqual(replacement_files(root)[0][WORKER], b'worker')
                for change in ('dirty', 'source', 'worker', 'binary', 'lock', 'inventory', 'command'):
                    bad = copy.deepcopy(report)
                    if change == 'dirty': bad['source_dirty'] = True
                    elif change == 'source': bad['commit'] = 'not-a-commit'
                    elif change in ('worker', 'binary'):
                        bad['files'][WORKER if change == 'worker' else 'bin/supabricks'] = 'tampered'
                    elif change == 'lock': bad['cargo_lock_sha256'] = 'other'
                    elif change == 'inventory': bad['files']['delta.py'] = digest(b'delta')
                    else: bad['command'] = ['echo', 'not built']
                    save(bad)
                    with self.subTest(change=change), self.assertRaises(AssertionError):
                        replacement_files(root)
                save(report)
                # A forged worker hash still cannot authorize uncommitted source.
                (root / WORKER).write_bytes(b'forged')
                report['files'][WORKER] = digest(b'forged'); save(report)
                with self.assertRaisesRegex(AssertionError, 'committed'):
                    replacement_files(root)

    def test_sail_overlay_needs_explicit_platform_artifact_for_worker_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = 'python/runtime/lib/python3.12/site-packages/pysail/_native.so'
            sail = {native: b'sail'}
            platform = {WORKER: b'worker', 'bin/supabricks': b'binary'}
            for name, data in dict(sail, **platform).items():
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
            (root / 'platform-build.json').write_text('{}')
            report = dict(version='0.7.1', commit='sail', wheel={'sha256': 'wheel'}, source_lock_sha256='lock')
            before = dict(version='v1', target='linux-x86_64', provenance={'sail': report},
                          files={name: dict(sha256='old', executable=name != WORKER) for name in dict(sail, **platform)})
            after = copy.deepcopy(before); after['version'] = 'v2'
            for name, data in dict(sail, **platform).items(): after['files'][name]['sha256'] = digest(data)
            with patch('sail_candidate.replacement_files', return_value=(sail, report)), patch(
                    'platform_candidate.replacement_files', return_value=(platform, {'commit': 'a' * 40})):
                with self.assertRaises(AssertionError): validate(before, after, root, root)
                self.assertEqual(validate(before, after, root, root, root)['platform_commit'], 'a' * 40)
                (root / WORKER).write_bytes(b'tampered')
                with self.assertRaises(AssertionError): validate(before, after, root, root, root)


if __name__ == '__main__': unittest.main()
