import json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0,'e2e/native/performance')
from native_package import overlay,sha
root=Path('build/eq03-recovery').resolve();prefix=root/'programs'
base=Path('build/eq03-capacity/runtime-02').resolve()
version=json.loads((base/'release.json').read_text())['version']
old=prefix/'releases'/version;new=prefix/'releases'/(version+'.eq196')
old.parent.mkdir(parents=True,exist_ok=True)
subprocess.run(['cp','-al',str(base),str(old)],check=True)
revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
overlay(base,Path('target/release/supabricks').resolve(),new,revision,root/'package-native.json')
manifest=json.loads((new/'release.json').read_text());manifest['version']=new.name
(new/'release.json').write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
(prefix/'bin').mkdir()
os.symlink('releases/'+old.name,prefix/'current')
for name in ['supabricks','psql']:os.symlink('../current/bin/'+name,prefix/'bin'/name)
verified=json.loads(subprocess.check_output([str(new/'bin/supabricks'),'installation','verify'],text=True))
proof=json.loads((root/'package-native.json').read_text())
proof.update(release_sha256=sha(new/'release.json'),installation_verification=verified,
             engineering_version=dict(before=old.name,after=new.name),signed_release=False)
(root/'package-upgrade.json').write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(dict(identity=verified['identity'],version=verified['version'],verified=verified['verified'])))
