import subprocess
import unittest
from unittest.mock import patch
from cleanup import stop


class CleanupTest(unittest.TestCase):
    def test_success_does_not_relabel_an_earlier_failure(self):
        report={'status':'failed'}
        with patch('cleanup.subprocess.run',return_value=subprocess.CompletedProcess([],0)):
            stop(report,['binary','down'],env={})
        self.assertEqual(report,{'status':'failed'})

    def test_exit_timeout_and_missing_binary_fail_without_command_secrets(self):
        for outcome in (subprocess.CompletedProcess([],2,stderr='private password'),
                        subprocess.TimeoutExpired(['private command'],90),FileNotFoundError('private path')):
            report={'status':'passed'}
            kwargs={'side_effect':outcome} if isinstance(outcome,Exception) else {'return_value':outcome}
            with self.subTest(outcome=type(outcome).__name__),patch('cleanup.subprocess.run',**kwargs):
                stop(report,['binary','private URI'],env={})
                self.assertEqual(report['status'],'failed')
                self.assertEqual(len(report['cleanup_errors']),1)
                self.assertNotIn('private',str(report))


if __name__ == '__main__':unittest.main()
