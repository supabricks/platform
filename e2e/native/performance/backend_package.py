#!/usr/bin/env python3
"""Immutable SQLite contract/owner overlay; identical dependencies and profiler."""
import argparse
import json
from pathlib import Path
import subprocess
from native_package import sha


def overlay(repo,base,destination,proof,candidate=False,native_binary=None):
    assert not destination.exists() and not proof.exists()
    manifest=json.loads((base/'release.json').read_text());original=sha(base/'release.json');changes={}
    for name,entry in manifest['files'].items():
        assert sha(base/name)==entry['sha256'],name
    subprocess.run(['cp','-al',str(base),str(destination)],check=True)
    def replace(name,data):
        path=destination/name;before=sha(path) if path.exists() else None
        executable=manifest['files'].get(name,{}).get('executable',False)
        if path.exists():path.unlink()
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o755 if executable else 0o644)
        manifest['files'][name]=dict(sha256=sha(path),executable=executable)
        changes[name]=dict(before=before,after=sha(path))
    assert candidate,'SP10a reuses the exact accepted predecessor package'
    sources={name:(repo/'python/analytics'/name).read_bytes() for name in
        ('capture/spool.py','capture/journal.py','capture/sqlite_journal.py','incremental/storage.py')}
    if native_binary:
        sources.update({name:(repo/'python/analytics'/name).read_bytes() for name in ('capture/owner.py','capture/ranges.py')})
        replace('bin/supabricks',native_binary.read_bytes())
    source=(repo/'python/analytics/capture_worker.py').read_text().splitlines(keepends=True)
    source.insert(2,'import worker_profile\n');source=''.join(source)
    marker="if __name__=='__main__':";assert source.count(marker)==1
    sources['capture_worker.py']=source.replace(marker,"worker_profile.install(globals(), 'capture')\n\n"+marker).encode()
    assert sha(base/'python/analytics/worker_profile.py')==sha(repo/'e2e/native/performance/worker_profile.py')
    for name,data in sources.items():
        relative='python/analytics/'+name;replace(relative,data)
        path=destination/relative;cache=path.parent/'__pycache__'/(path.stem+'.cpython-312.pyc')
        before=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(destination/'python/analytics/python'),'-B','-c',
            'import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)',str(path),relative],check=True)
        relative=str(cache.relative_to(destination));manifest['files'][relative]=dict(sha256=sha(cache),executable=False)
        changes[relative]=dict(before=before,after=sha(cache))
    path=destination/'release.json';path.unlink();path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    assert sha(base/'release.json')==original
    for name,entry in changes.items():assert (sha(base/name) if (base/name).exists() else None)==entry['before']
    verification=json.loads(subprocess.check_output([str(destination/'bin/supabricks'),'installation','verify'],text=True))
    assert verification['verified'] and verification['identity']==sha(path)
    assert sha(destination/'bin/supabricks')==sha(native_binary or base/'bin/supabricks')
    assert sha(destination/'python/analytics/worker_profile.py')==sha(base/'python/analytics/worker_profile.py')
    proof.write_text(json.dumps(dict(base_release_sha256=original,release_sha256=sha(path),candidate=candidate,
        revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        changes=changes,installation_verification=verification,signed_release=False),indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','base','destination','proof'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--candidate',action='store_true');p.add_argument('--native-binary',type=Path);a=p.parse_args()
    overlay(a.repo.resolve(),a.base.resolve(),a.destination.resolve(),a.proof.resolve(),a.candidate,a.native_binary.resolve() if a.native_binary else None)
