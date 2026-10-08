import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0,'e2e/tpcds')
from package_platform_candidate import package
root=Path('build/eq197').resolve();prefix=root/'programs'
original=Path('build/eq03-capacity/runtime-02').resolve()
base=Path('build/eq199/programs/releases/v0.1.0-alpha.36.eq199').resolve()
old=prefix/'releases/v0.1.0-alpha.36';new=prefix/'releases/v0.1.0-alpha.36.eq197b'
old.parent.mkdir(parents=True,exist_ok=True)
assert not old.exists()
subprocess.run(['cp','-al',str(original),str(old)],check=True)
package(base,original,new,Path('build/eq199/sail-artifact').resolve(),root/'platform-artifact',root/'package.json')
(prefix/'bin').mkdir();os.symlink('releases/'+old.name,prefix/'current')
for name in ['supabricks','psql']:os.symlink('../current/bin/'+name,prefix/'bin'/name)
