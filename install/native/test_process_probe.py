import unittest
from unittest.mock import Mock
import psutil
from process_probe import cmdline


class ProcessProbe(unittest.TestCase):
    def test_upstream_procargs_error_becomes_access_denied(self):
        process=Mock(pid=42)
        error=SystemError('<built-in function proc_cmdline> returned a result with an exception set')
        error.__cause__=PermissionError('private diagnostic')
        process.cmdline.side_effect=error
        with self.assertRaises(psutil.AccessDenied):cmdline(process)

    def test_unrelated_system_errors_are_not_suppressed(self):
        for error in (SystemError('other'),SystemError('<built-in function proc_cmdline> returned a result with an exception set')):
            process=Mock()
            process.cmdline.side_effect=error
            with self.assertRaises(SystemError):cmdline(process)


if __name__=='__main__':unittest.main()
