import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import checks


class ScopeTests(unittest.TestCase):
    def test_native_fixtures_are_not_root_cluster_scripts(self):
        for name in ('e2e/native/metrics.py', 'e2e/native/catalog/service.py', 'e2e/tpcds/inputs.py'):
            self.assertFalse(checks.select([name])['cluster'], name)
        for name in ('e2e/run.sh', 'e2e/restore.sh'):
            self.assertTrue(checks.select([name])['cluster'], name)
        self.assertTrue(checks.matches('crates/local/src/metrics/history.rs', 'crates/local/**'))

    def test_postgres_does_not_launch_cluster_or_analytical_baseline(self):
        result = checks.select(['crates/local/src/metrics/history.rs'])
        self.assertTrue(result['portable'])
        self.assertTrue(result['fmt'])
        for name in ('cluster', 'analytics', 'ui', 'console', 'operator'):
            self.assertFalse(result[name], name)

    def test_transitive_and_unknown_inputs_are_conservative(self):
        for path in ('Cargo.lock', '.cargo/config.toml', 'new-runtime/config.json'):
            self.assertTrue(all(checks.select([path]).values()), path)
        shared = checks.select(['crates/core/src/lib.rs'])
        for name in ('portable', 'operator', 'cluster'):
            self.assertTrue(shared[name])
        for path in ('components/deltalake-source.lock.json',
                     'components/components.lock.json',
                     'components/patches/deltalake-bounded-merge.patch',
                     '.github/actions/deltalake/action.yml', 'python/analytics/uv.lock'):
            self.assertTrue(checks.select([path])['analytics'], path)

    def test_prose_and_frontend_scope(self):
        self.assertFalse(any(checks.select(['docs/handbook/dev-loop.md', 'README.md']).values()))
        console = checks.select(['console'])
        self.assertTrue(console['unit'] and console['console'])
        self.assertFalse(console['portable'] or console['cluster'] or console['analytics'])
        self.assertTrue(checks.select(['docs/architecture/evidence.json'])['components'])

    def test_local_commands_do_not_repeat_packages_or_run_runtime_qualification(self):
        commands = checks.local_commands(checks.select(['Cargo.lock']))
        cargo_tests = [c for c in commands if c[:2] == ['cargo', 'test']]
        self.assertEqual(len(cargo_tests), 2)
        self.assertIn('--lib', cargo_tests[0])
        self.assertEqual(cargo_tests[0].count('supabricks-local'), 1)
        self.assertIn('--bins', cargo_tests[1])
        self.assertFalse(any('docker' in c or 'e2e/run.sh' in c for c in commands))

    def test_edited_integration_test_is_selected_without_a_workspace_sweep(self):
        paths = ['crates/local/tests/recovery.rs']
        commands = checks.local_commands(checks.select(paths), paths)
        selected = [c for c in commands if '--test' in c]
        self.assertEqual(selected, [['cargo', 'test', '--locked', '-p', 'supabricks-local', '--test', 'recovery']])

    def test_git_selection_includes_both_rename_sides_deletes_and_untracked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return checks.git(*args, root=root)
            git('init', '-q'); git('config', 'user.email', 'test@example.invalid'); git('config', 'user.name', 'Test')
            (root/'crates/local').mkdir(parents=True)
            (root/'crates/local/input.rs').write_text('old')
            (root/'remove').write_text('old')
            git('add', '.'); git('commit', '-qm', 'base')
            (root/'docs').mkdir()
            git('mv', 'crates/local/input.rs', 'docs/input.md')
            git('rm', '-q', 'remove')
            (root/'untracked.py').write_text('pass')
            self.assertEqual(checks.changed('HEAD', root=root),
                             ['crates/local/input.rs', 'docs/input.md', 'remove', 'untracked.py'])
            self.assertTrue(checks.select(checks.changed('HEAD', root=root))['portable'])

    def test_missing_diff_selects_all_in_github(self):
        with tempfile.TemporaryDirectory() as folder:
            event = Path(folder)/'event.json'
            event.write_text(json.dumps({'before': 'a'*40}))
            with patch.dict(os.environ, GITHUB_EVENT_PATH=str(event)), patch('checks.subprocess.run',
                    side_effect=subprocess.CalledProcessError(1, 'git')):
                self.assertTrue(all(checks.github_scope()[1].values()))


if __name__ == '__main__':
    unittest.main()
