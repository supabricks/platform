"""SP01 diagnostic overlay; inherited components/probes remain byte-identical."""
import argparse,hashlib,json,subprocess
from pathlib import Path
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--base',type=Path,required=True)
parser.add_argument('--destination',type=Path,required=True)
parser.add_argument('--proof',type=Path,required=True)
args=parser.parse_args()
repo=args.repo.resolve();base=args.base.resolve();dest=args.destination.resolve()
assert not dest.exists()
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
original=sha(base/'release.json');manifest=json.loads((base/'release.json').read_text());changes={}
subprocess.run(['cp','-al',str(base),str(dest)],check=True)
def replace(name,data):
    p=dest/name;before=sha(p) if p.exists() else None
    if p.exists():p.unlink()
    p.write_bytes(data);p.chmod(0o755 if manifest['files'][name]['executable'] else 0o644)
    manifest['files'][name]['sha256']=sha(p);changes[name]=dict(before=before,after=sha(p))
replace('bin/supabricks',(repo/'target/release/supabricks').read_bytes())
replace('python/analytics/incremental/storage.py',(repo/'python/analytics/incremental/storage.py').read_bytes())
s=(repo/'python/analytics/incremental_worker.py').read_text();lines=s.splitlines(keepends=True);lines.insert(2,'import worker_profile\n');s=''.join(lines)
s=s.replace("if __name__=='__main__':","worker_profile.install(globals(), 'incremental')\n\nif __name__=='__main__':")
replace('python/analytics/incremental_worker.py',s.encode())
for name in ('python/analytics/incremental/storage.py','python/analytics/incremental_worker.py'):
    source=dest/name;cache=source.parent/'__pycache__'/(source.stem+'.cpython-312.pyc');relative=str(cache.relative_to(dest));before=sha(cache);cache.unlink()
    subprocess.run([str(dest/'python/analytics/python'),'-B','-c',"import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)",str(source),name],check=True)
    manifest['files'][relative]['sha256']=sha(cache);changes[relative]=dict(before=before,after=sha(cache))
p=dest/'release.json';p.unlink();p.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
assert sha(base/'release.json')==original
for name,entry in changes.items():assert sha(base/name)==entry['before']
proof=dict(base_release_sha256=original,candidate_release_sha256=sha(p),runtime_revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),build=['cargo','build','--locked','--release','-p','supabricks-local','--features','sync-profile','--bin','supabricks'],changes=changes,probe_change=False,bytecode='checked-hash; changed modules only; relative diagnostic filenames')
args.proof.write_text(json.dumps(proof,indent=2)+'\n')
print(json.dumps(proof,indent=2))
