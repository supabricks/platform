import json
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
