"""Validate the installed exact superset, including version and package drift."""
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

release = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(release / 'python/analytics'))
import runtime_environment


class InstalledEnvironment(unittest.TestCase):
    def test_exact_environment(self):
        _, installed = runtime_environment.environment()
        self.assertEqual(installed['jupyter-server'], '2.21.0')

    def test_rejects_added_removed_and_changed_packages(self):
        distributions = list(runtime_environment.importlib.metadata.distributions())
        extra = types.SimpleNamespace(metadata={'Name': 'unqualified-extension'}, version='1.0')
        changed = [types.SimpleNamespace(metadata=d.metadata, version='0.0' if d.metadata['Name'] == 'pyarrow' else d.version) for d in distributions]
        removed = [d for d in distributions if d.metadata['Name'] != 'jupyter-server']
        for values in [distributions + [extra], changed, removed]:
            with self.subTest(packages=len(values)), patch.object(runtime_environment.importlib.metadata, 'distributions', return_value=values):
                with self.assertRaises(RuntimeError):
                    runtime_environment.environment()


if __name__ == '__main__':
    unittest.main()
