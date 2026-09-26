#!/usr/bin/env python3
"""SP02 Python-only diagnostic overlay on an already instrumented SP01 package."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def overlay(repo,base,dest,proof,predecessor=False):
    assert not dest.exists()
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    original=sha(base/'release.json');manifest=json.loads((base/'release.json').read_text());changes={}
    assert (base/'python/analytics/profile_io.so').is_file()
    subprocess.run(['cp','-al',str(base),str(dest)],check=True)
    def replace(name,data):
        p=dest/name;before=sha(p) if p.exists() else None
        executable=manifest['files'].get(name,{}).get('executable',False)
        if p.exists():p.unlink()
        p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);p.chmod(0o755 if executable else 0o644)
        manifest['files'][name]=dict(executable=executable,sha256=sha(p));changes[name]=dict(before=before,after=sha(p))
    sources={'worker_profile.py':(repo/'e2e/native/performance/worker_profile.py').read_bytes()}
    if not predecessor:
        for name in ('capture/spool.py','capture/protocol.py','capture/groups.py'):
            sources[name]=(repo/'python/analytics'/name).read_bytes()
        s=(repo/'python/analytics/capture_worker.py').read_text();lines=s.splitlines(keepends=True);lines.insert(2,'import worker_profile\n');s=''.join(lines)
        marker="if __name__=='__main__':";assert s.count(marker)==1
        sources['capture_worker.py']=s.replace(marker,"worker_profile.install(globals(), 'capture')\n\n"+marker).encode()
    for name,data in sources.items():
        relative='python/analytics/'+name;replace(relative,data)
        source=dest/relative;cache=source.parent/'__pycache__'/(source.stem+'.cpython-312.pyc')
        before=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(dest/'python/analytics/python'),'-B','-c',"import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)",str(source),relative],check=True)
        rel=str(cache.relative_to(dest));manifest['files'][rel]=dict(executable=False,sha256=sha(cache));changes[rel]=dict(before=before,after=sha(cache))
    p=dest/'release.json';p.unlink();p.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    assert sha(base/'release.json')==original
    for name,entry in changes.items():
        assert (sha(base/name) if (base/name).exists() else None)==entry['before']
    assert sha(dest/'bin/supabricks')==sha(base/'bin/supabricks')
    result=dict(base_release_sha256=original,release_sha256=sha(p),predecessor=predecessor,
        overlay_revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        binary='byte-identical SP01 diagnostic binary; no Rust changes',changes=changes,
        bytecode='checked-hash, relative filenames; no dependency changes',signed_release=False)
    proof.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','base','destination','proof'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--predecessor',action='store_true');a=p.parse_args()
    overlay(a.repo.resolve(),a.base.resolve(),a.destination.resolve(),a.proof,a.predecessor)
