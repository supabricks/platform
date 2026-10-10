"""Enable opt-in diagnostics before starting the frozen installed load harness."""
import runpy,sys
from pathlib import Path
harness=Path(sys.argv[1]);sys.argv=[str(harness/'e2e/tpcds/load.py'),*sys.argv[2:]]
sys.path.insert(0,str(harness/'e2e/native'))
sys.path.insert(0,str(harness/'e2e/tpcds'))
from installed_sync import InstalledContinuous
original=InstalledContinuous.start

def start(self):
    directory=self.root/'sync-profile';directory.mkdir(mode=0o700,exist_ok=True)
    (directory/'enabled').touch(mode=0o600)
    return original(self)
InstalledContinuous.start=start
runpy.run_path(sys.argv[0],run_name='__main__')
