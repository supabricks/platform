"""Enable diagnostics around an unchanged, frozen load harness."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

load=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(load.parent.parent/'native'))
sys.path.insert(0,str(load.parent))
from installed_sync import InstalledContinuous
original=InstalledContinuous.__init__

def initialize(self,release,root):
    original(self,release,root)
    profile=Path(root)/'sync-profile';profile.mkdir(mode=0o700)
    (profile/'enabled').write_text('EQ230 diagnostic events; no operating-policy changes\n')
    (profile/'fixture.json').write_text(json.dumps(dict(load_sha256=hashlib.sha256(load.read_bytes()).hexdigest(),
        wrapper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2)+'\n')

InstalledContinuous.__init__=initialize
sys.argv=sys.argv[1:]
runpy.run_path(str(load),run_name='__main__')
