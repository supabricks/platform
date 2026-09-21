"""Predecessor recovery must never mask unrelated or repeated apply failures."""
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from project_portability import seed_predecessor


class PredecessorTest(unittest.TestCase):
    def cell(self, error='database preparation failed or was superseded'):
        cell = Mock()
        cell.apply.side_effect = [dict(id='apply-id', state='failed', error=error,
                                     next_step=0, plan={'steps': [dict(logical='database.main', kind='database', action='create')]},
                                     resources={'database.main': {}}), dict(state='succeeded')]
        cell.cli.side_effect = [dict(active_revision=None), dict(branches=[{}]),
                                dict(revision=1, observed_revision=0), dict(revision=1, observed_revision=1),
                                {}, dict(branches=[{}])]
        return cell

    def test_success_needs_no_recovery(self):
        cell = Mock(); cell.apply.return_value = dict(state='succeeded')
        self.assertFalse(seed_predecessor(cell, Path('/project')))
        cell.cli.assert_not_called()

    def test_known_failure_reconciles_before_one_explicit_retry(self):
        cell = self.cell()
        with patch('project_portability.time.sleep'):
            self.assertTrue(seed_predecessor(cell, Path('/project')))
        self.assertEqual(cell.apply.call_count, 2)
        self.assertEqual(cell.apply.call_args.args[1], 'predecessor-reconciled')
        self.assertEqual(cell.cli.call_count, 6)

    def test_database_failure_before_receipt_is_reconciled_once(self):
        cell=self.cell()
        first=next(cell.apply.side_effect)
        first['resources']={}
        cell.apply.side_effect=[first,dict(state='succeeded')]
        with patch('project_portability.time.sleep'):
            self.assertTrue(seed_predecessor(cell,Path('/project')))
        self.assertEqual(cell.apply.call_count,2)

    def environment_failure(self):
        cell = self.cell()
        operation = next(cell.apply.side_effect)
        operation['next_step'] = 1
        operation['plan']['steps'].insert(0, dict(logical='environment.notebook', kind='environment',
                                                action='prepare_offline', environment='notebook'))
        operation['resources']['environment.notebook'] = dict(
            kind='environment', environment='notebook', origin='apply-id',
            generation='00000000-0000-4000-8000-000000000001')
        cell.apply.side_effect = [operation, dict(state='succeeded')]
        return cell, operation

    def test_completed_preceding_environment_allows_database_reconciliation(self):
        cell, _ = self.environment_failure()
        with patch('project_portability.time.sleep'):
            self.assertTrue(seed_predecessor(cell, Path('/project')))
        self.assertEqual(cell.apply.call_count, 2)

    def test_invalid_predecessor_boundary_is_not_retried(self):
        for change in ('later_step', 'later_receipt', 'missing_environment', 'foreign_origin', 'missing_generation', 'wrong_order'):
            with self.subTest(change=change):
                cell, operation = self.environment_failure()
                if change == 'later_step':
                    operation['plan']['steps'][1]['kind'] = 'migration'
                elif change == 'later_receipt':
                    operation['resources']['migration.seed'] = {}
                elif change == 'missing_environment':
                    del operation['resources']['environment.notebook']
                elif change == 'foreign_origin':
                    operation['resources']['environment.notebook']['origin'] = 'another-apply'
                elif change == 'missing_generation':
                    operation['resources']['environment.notebook']['generation'] = ''
                else:
                    operation['plan']['steps'][0]['action'] = 'install'
                with self.assertRaises((AssertionError, ValueError)):
                    seed_predecessor(cell, Path('/project'))
                cell.cli.assert_not_called()
                self.assertEqual(cell.apply.call_count, 1)

    def test_unrelated_failure_is_not_retried(self):
        cell = self.cell('environment preparation failed')
        with self.assertRaisesRegex(AssertionError, 'unexpected predecessor'):
            seed_predecessor(cell, Path('/project'))
        cell.cli.assert_not_called()
        self.assertEqual(cell.apply.call_count, 1)

    def test_retry_failure_is_not_swallowed(self):
        cell = self.cell()
        first = next(cell.apply.side_effect)
        cell.apply.side_effect = [first, AssertionError('retry failed')]
        with patch('project_portability.time.sleep'), self.assertRaisesRegex(AssertionError, 'retry failed'):
            seed_predecessor(cell, Path('/project'))
        self.assertEqual(cell.apply.call_count, 2)

    def test_recovery_requires_unactivated_database_only(self):
        for change in ('environment', 'active'):
            cell = self.cell()
            if change == 'environment':
                first = next(cell.apply.side_effect)
                first['resources']['environment.notebook'] = {}
                cell.apply.side_effect = [first]
            else:
                cell.cli.side_effect = [dict(active_revision='already-active')]
            with self.assertRaises(AssertionError):
                seed_predecessor(cell, Path('/project'))
            self.assertEqual(cell.apply.call_count, 1)


class PressureCleanupTest(unittest.TestCase):
    def test_only_dedicated_mount_roots_are_accepted(self):
        script = Path(__file__).with_name('detach_macos_volume.sh')
        # Absent roots return before stat/hdiutil, so this also runs on Linux.
        for path, accepted in (('/tmp/sb-pk07-volume.absent-test', True),
                               ('/tmp/sb-i01-volume.absent-test', True),
                               ('/tmp/sb-pk07-volume.absent-test/child', False),
                               ('/tmp/ordinary-volume', False)):
            self.assertFalse(Path(path).exists())
            result = subprocess.run(['bash', str(script), path], capture_output=True)
            self.assertEqual(result.returncode == 0, accepted)


if __name__ == '__main__': unittest.main()
