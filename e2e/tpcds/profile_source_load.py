"""Wrap the frozen concurrent loader with opt-in EQ232 source observation."""
import hashlib
import json
from pathlib import Path
import runpy
import sys

load = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(load.parent.parent / 'native'))
sys.path.insert(0, str(load.parent))
from installed_sync import InstalledContinuous
from source_profile import Observer
original_init = InstalledContinuous.__init__
original_stop = InstalledContinuous.stop


def initialize(self, release, root):
    original_init(self, release, root)
    self.source_observer = Observer(self, self.source, Path(root).parent / 'source-profile')
    self.source = self.source_observer.source
    profile = Path(root) / 'sync-profile'; profile.mkdir(mode=0o700)
    (profile / 'enabled').write_text('EQ232 diagnostic; unchanged sync policy\n')
    (profile / 'fixture.json').write_text(json.dumps(dict(load_sha256=hashlib.sha256(load.read_bytes()).hexdigest(),
        wrapper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()), indent=2) + '\n')


def stop(self):
    try:
        self.source_observer.finish()
    finally:
        original_stop(self)


InstalledContinuous.__init__ = initialize
InstalledContinuous.stop = stop
sys.argv = sys.argv[1:]
runpy.run_path(str(load), run_name='__main__')
