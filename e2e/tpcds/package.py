#!/usr/bin/env python3
"""Verified Python-only engineering overlay; never a signed-release claim."""
import argparse
import json
from pathlib import Path
import subprocess
from inputs import sha


def overlay(base,destination,repo,proof,names):
    if destination.exists() or proof.exists():raise ValueError('fresh paths required')
    manifest=json.loads((base/'release.json').read_text());identity=sha(base/'release.json')
    for name,entry in manifest['files'].items():
        p=base/name
        if sha(p)!=entry['sha256'] or bool(p.stat().st_mode&0o111)!=entry['executable']:
            raise ValueError('base package differs: '+name)
    subprocess.run(['cp','-al',str(base),str(destination)],check=True)
    changes={}
    for name in names:
        source=repo/'python/analytics'/name
        if not source.resolve().is_relative_to((repo/'python/analytics').resolve()) or source.suffix!='.py':
            raise ValueError('analytics Python source required')
        relative='python/analytics/'+name;p=destination/relative
        before=sha(p);mode=p.stat().st_mode&0o777
        p.unlink();p.write_bytes(source.read_bytes());p.chmod(mode)
        cache=p.parent/'__pycache__'/(p.stem+'.cpython-312.pyc')
        old_cache=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(destination/'python/analytics/python'),'-B','-c',
            'import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)',str(p),relative],check=True)
        for path,old in [(p,before),(cache,old_cache)]:
            key=str(path.relative_to(destination));manifest['files'][key]=dict(sha256=sha(path),executable=bool(path.stat().st_mode&0o111))
            changes[key]=dict(before=old,after=sha(path))
    p=destination/'release.json';p.unlink();p.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    if sha(base/'release.json')!=identity:raise ValueError('base changed')
    for name,entry in changes.items():
        if (sha(base/name) if (base/name).exists() else None)!=entry['before']:raise ValueError('base payload changed')
    verification=json.loads(subprocess.check_output([str(destination/'bin/supabricks'),'installation','verify'],text=True))
    assert verification['verified'] and verification['identity']==sha(p)
    proof.write_text(json.dumps(dict(scope='Python-only engineering overlay; unsigned',base_identity=identity,
        identity=sha(p),changes=changes,verified_base_files=len(manifest['files']),verification=verification),indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('base','destination','repo','proof'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--source',action='append',required=True)
    a=p.parse_args();overlay(a.base.resolve(),a.destination.resolve(),a.repo.resolve(),a.proof.resolve(),a.source)
