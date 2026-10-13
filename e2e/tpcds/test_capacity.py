import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from capacity import CapacityChecks


class CapacityEvidenceTests(unittest.TestCase):
    def test_failed_history_retains_measured_peak_without_passing_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture=CapacityChecks(root,root)
            def child(command,**kwargs):
                if command[-1]=='history':
                    (root/'measurements.json').write_text(json.dumps({'history':{'highwater_bytes':900*1024**2}}))
                    raise subprocess.CalledProcessError(1,command)
            with patch('capacity.subprocess.run',side_effect=child):
                with self.assertRaises(subprocess.CalledProcessError):fixture.run('python','export.py')
            self.assertEqual(fixture.metrics['phase'],'history')
            self.assertEqual(fixture.metrics['history']['highwater_bytes'],900*1024**2)
            self.assertEqual(fixture.checks,[])
