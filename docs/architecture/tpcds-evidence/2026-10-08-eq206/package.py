import json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0,'e2e/tpcds')
from package_sail_candidate import package
root=Path('build/eq206').resolve();prefix=root/'programs'
base=Path('build/eq198/programs-final/releases/v0.1.0-alpha.36.eq198c').resolve();original=Path('build/eq03-capacity/runtime-02').resolve()
old=prefix/'releases/v0.1.0-alpha.36';new=prefix/'releases/v0.1.0-alpha.36.eq206c'
old.parent.mkdir(parents=True,exist_ok=True);assert not old.exists()
subprocess.run(['cp','-al',str(original),str(old)],check=True)
package(base,new,root/'sail-artifact',root/'package.json')
(prefix/'bin').mkdir();os.symlink('releases/'+old.name,prefix/'current')
for name in ['supabricks','psql']:os.symlink('../current/bin/'+name,prefix/'bin'/name)
