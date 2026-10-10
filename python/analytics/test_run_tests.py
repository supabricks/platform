"""A failing or hung isolated module must still fail the aggregate gate."""
import tempfile
from pathlib import Path
import unittest
from run_tests import run


class RunnerTests(unittest.TestCase):
    def test_failure_is_retained_and_other_modules_still_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'test_a.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_fail(self): self.fail("expected fixture failure")\n')
            (root/'test_b.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_pass(self): self.assertTrue(True)\n')
            results=run(root)
            self.assertEqual([r['exit_code'] for r in results],[1,0])
            self.assertFalse(any(r['timed_out'] for r in results))

    def test_hung_module_is_bounded_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'test_hung.py').write_text('import time\ntime.sleep(60)\n')
            result=run(root,timeout=.2)[0]
            self.assertTrue(result['timed_out']);self.assertEqual(result['exit_code'],124)
            self.assertLess(result['seconds'],5)

    def test_empty_selection_cannot_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):run(Path(tmp))
            (Path(tmp)/'test_empty.py').write_text('')
            self.assertEqual(run(Path(tmp))[0]['exit_code'],1)


if __name__=='__main__':unittest.main()
