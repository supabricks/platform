"""Keep the installed adapter's real check output compatible with release evidence."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from installed_sync import SUITES

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'install/native'))
from sync_evidence import collect,REQUIRED
from test_sync_evidence import fixture,REVISION


class InstalledEvidenceContract(unittest.TestCase):
    def test_all_suite_adapters_serialize_checks_accepted_by_collector(self):
        data,base=fixture()
        for name,suite in SUITES.items():
            cell=suite.__new__(suite);cell.checks=[]
            with contextlib.redirect_stdout(io.StringIO()):
                for check in sorted(REQUIRED[name]):cell.check(check)
            # Source harnesses emit objects; installed collectors require names.
            # Exercise the actual adapter method and serialization, not a second
            # hand-built list that could hide a producer/consumer mismatch.
            data['suites'][name]['checks']=json.loads(json.dumps(cell.checks))
        self.assertEqual(collect(data,base,REVISION,'linux-x86_64')['status'],'passed')


if __name__=='__main__':unittest.main()
