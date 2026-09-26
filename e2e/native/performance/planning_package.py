#!/usr/bin/env python3
"""Immutable SP04 overlays; common profiler, unchanged native code/dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def overlay(repo,base,dest,proof,predecessor=False):
    assert not dest.exists()
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    original=sha(base/'release.json');manifest=json.loads((base/'release.json').read_text());changes={}
    subprocess.run(['cp','-al',str(base),str(dest)],check=True)
    def replace(name,data):
        p=dest/name;before=sha(p) if p.exists() else None
        executable=manifest['files'].get(name,{}).get('executable',False)
        if p.exists():p.unlink()
        p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);p.chmod(0o755 if executable else 0o644)
        manifest['files'][name]=dict(executable=executable,sha256=sha(p));changes[name]=dict(before=before,after=sha(p))
    sources={'worker_profile.py':(repo/'e2e/native/performance/worker_profile.py').read_bytes()}
    if not predecessor:
        sources['incremental/planning.py']=(repo/'python/analytics/incremental/planning.py').read_bytes()
        s=(repo/'python/analytics/incremental_worker.py').read_text();lines=s.splitlines(keepends=True);lines.insert(2,'import worker_profile\n');s=''.join(lines)
        marker="if __name__=='__main__':";assert s.count(marker)==1
        sources['incremental_worker.py']=s.replace(marker,"worker_profile.install(globals(), 'incremental')\n\n"+marker).encode()
    for name,data in sources.items():
        relative='python/analytics/'+name;replace(relative,data)
        source=dest/relative;cache=source.parent/'__pycache__'/(source.stem+'.cpython-312.pyc')
        before=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(dest/'python/analytics/python'),'-B','-c',"import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)",str(source),relative],check=True)
        rel=str(cache.relative_to(dest));manifest['files'][rel]=dict(executable=False,sha256=sha(cache));changes[rel]=dict(before=before,after=sha(cache))
    p=dest/'release.json';p.unlink();p.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    assert sha(base/'release.json')==original
    for name,entry in changes.items():assert (sha(base/name) if (base/name).exists() else None)==entry['before']
    assert sha(dest/'bin/supabricks')==sha(base/'bin/supabricks')
    proof.write_text(json.dumps(dict(base_release_sha256=original,release_sha256=sha(p),predecessor=predecessor,
        overlay_revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        binary_sha256=sha(dest/'bin/supabricks'),changes=changes,signed_release=False,
        bytecode='checked-hash, relative filenames; no dependency changes'),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','base','destination','proof'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--predecessor',action='store_true');a=p.parse_args()
    overlay(a.repo.resolve(),a.base.resolve(),a.destination.resolve(),a.proof,a.predecessor)
