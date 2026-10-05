#!/usr/bin/env python3
"""Immutable #157 overlay: capture maintenance only, with identical observers."""
import argparse
import json
from pathlib import Path
import subprocess
from native_package import sha


def overlay(repo,base,destination,proof):
    assert not destination.exists() and not proof.exists()
    manifest=json.loads((base/'release.json').read_text());original=sha(base/'release.json');changes={}
    for name,entry in manifest['files'].items():assert sha(base/name)==entry['sha256'],name
    subprocess.run(['cp','-al',str(base),str(destination)],check=True)
    worker=(repo/'python/analytics/capture_worker.py').read_text().splitlines(keepends=True)
    worker.insert(2,'import worker_profile\n');worker=''.join(worker)
    marker="if __name__=='__main__':";assert worker.count(marker)==1
    worker=worker.replace(marker,"import marker_profile\nmarker_profile.install(globals())\nworker_profile.install(globals(), 'capture')\n\n"+marker)
    # Instrumentation must remain byte-identical to the predecessor.
    for name in ('worker_profile.py','marker_profile.py'):
        assert sha(base/'python/analytics'/name)==sha(repo/'e2e/native/performance'/name)
    for name,data in [('capture/spool.py',(repo/'python/analytics/capture/spool.py').read_bytes()),('capture_worker.py',worker.encode())]:
        relative='python/analytics/'+name;path=destination/relative
        before=sha(path);executable=manifest['files'][relative]['executable']
        path.unlink();path.write_bytes(data);path.chmod(0o755 if executable else 0o644)
        manifest['files'][relative]=dict(sha256=sha(path),executable=executable)
        changes[relative]=dict(before=before,after=sha(path))
        cache=path.parent/'__pycache__'/(path.stem+'.cpython-312.pyc');relative_cache=str(cache.relative_to(destination))
        before_cache=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(destination/'python/analytics/python'),'-c',
            'import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)',str(path),relative],check=True)
        manifest['files'][relative_cache]=dict(sha256=sha(cache),executable=False)
        changes[relative_cache]=dict(before=before_cache,after=sha(cache))
    path=destination/'release.json';path.unlink();path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    assert sha(base/'release.json')==original
    for name,change in changes.items():assert (sha(base/name) if (base/name).exists() else None)==change['before']
    assert sha(base/'bin/supabricks')==sha(destination/'bin/supabricks')
    verification=json.loads(subprocess.check_output([str(destination/'bin/supabricks'),'installation','verify'],text=True))
    assert verification['verified'] and verification['identity']==sha(path)
    proof.write_text(json.dumps(dict(base_release_sha256=original,release_sha256=sha(path),
        revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        binary_sha256=sha(destination/'bin/supabricks'),changes=changes,signed_release=False,
        installation_verification=verification),indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','base','destination','proof'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    overlay(args.repo.resolve(),args.base.resolve(),args.destination.resolve(),args.proof.resolve())
