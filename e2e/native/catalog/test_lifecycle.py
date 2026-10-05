from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import psutil
import recovery
import service


class Lifecycle(unittest.TestCase):
    def test_restart_poll_requires_new_registered_owner(self):
        cell=Mock()
        cell.request.return_value={'catalog':{'ready':True}}
        cell.records.side_effect=[[],[{'role':'unity-catalog','pid':12}],[{'role':'unity-catalog','pid':13}]]
        self.assertIsNone(service.ready_owner(cell,12))
        self.assertIsNone(service.ready_owner(cell,12))
        self.assertEqual(service.ready_owner(cell,12),{'ready':True})

    def test_private_arguments_are_actually_inspected_after_denial(self):
        process=Mock(pid=12)
        process.cmdline.side_effect=[psutil.AccessDenied(12),['java','-Djdk.net.hosts.file=private']]
        self.assertEqual(service.private_jre_cmdline(process),['java','-Djdk.net.hosts.file=private'])

    def test_restore_exit_fails_immediately_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            process=Mock()
            process.poll.return_value=1
            with patch.object(recovery.subprocess,'Popen',return_value=process):
                with self.assertRaisesRegex(AssertionError,'exit 1'):
                    recovery.interrupt_restore('binary',root/'backup',root/'restore',root/'private.log')
            process.wait.assert_called_once_with(timeout=10)
            process.kill.assert_not_called()

    def test_restore_timeout_kills_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            process=Mock()
            process.poll.return_value=None
            with patch.object(recovery.subprocess,'Popen',return_value=process),patch.object(recovery,'wait',side_effect=RuntimeError('timeout')):
                with self.assertRaisesRegex(RuntimeError,'timeout'):
                    recovery.interrupt_restore('binary',root/'backup',root/'restore',root/'private.log')
            process.kill.assert_called_once()
            process.wait.assert_called_once_with(timeout=10)


if __name__=='__main__':unittest.main()
