import json
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import psutil
import catalog_gate

class CensusTests(unittest.TestCase):
    def test_protected_unrelated_process_does_not_abort_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            child=Mock(pid=101)
            child.poll.return_value=0
            child.wait.return_value=0
            owned=Mock(pid=101)
            owned.children.return_value=[]
            denied=Mock()
            denied.cmdline.side_effect=PermissionError('protected process')
            missing=Mock()
            missing.cmdline.side_effect=psutil.NoSuchProcess(102)
            args=SimpleNamespace(command=['fixture'],data_root=root,timeout=5,report=root/'cleanup.json')
            with patch.object(catalog_gate.subprocess,'Popen',return_value=child), patch.object(catalog_gate.psutil,'Process',return_value=owned), patch.object(catalog_gate.psutil,'process_iter',return_value=[denied,missing]):
                self.assertEqual(catalog_gate.run(args),0)
            self.assertEqual(json.loads(args.report.read_text())['remaining_descendants'],0)

    def test_unexpected_inspection_failure_still_stops_fixture_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            child=Mock(pid=101)
            child.poll.return_value=None
            child.wait.return_value=-9
            args=SimpleNamespace(command=['fixture'],data_root=root,timeout=5,report=root/'cleanup.json')
            with patch.object(catalog_gate.subprocess,'Popen',return_value=child),patch.object(catalog_gate.psutil,'Process',side_effect=RuntimeError('private detail')),patch('sys.stderr',new_callable=io.StringIO):
                self.assertEqual(catalog_gate.run(args),1)
            child.kill.assert_called_once()
            child.wait.assert_called_once()
            report=json.loads(args.report.read_text())
            self.assertTrue(report['inspection_failed'])
            self.assertNotIn('private detail',str(report))
