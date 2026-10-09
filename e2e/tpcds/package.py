#!/usr/bin/env python3
"""Verified Python/native engineering overlay; never a signed-release claim."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile
from inputs import sha


def overlay(base,destination,repo,proof,names,sail_artifact=None,delta_artifact=None):
    if destination.exists() or proof.exists():raise ValueError('fresh paths required')
    manifest=json.loads((base/'release.json').read_text());identity=sha(base/'release.json');base_files=len(manifest['files'])
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
        before=sha(p) if p.exists() else None
        mode=p.stat().st_mode&0o777 if p.exists() else source.stat().st_mode&0o777
        if p.exists():p.unlink()
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(source.read_bytes());p.chmod(mode)
        cache=p.parent/'__pycache__'/(p.stem+'.cpython-312.pyc')
        old_cache=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(destination/'python/analytics/python'),'-B','-c',
            'import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)',str(p),relative],check=True)
        for path,old in [(p,before),(cache,old_cache)]:
            key=str(path.relative_to(destination));manifest['files'][key]=dict(sha256=sha(path),executable=bool(path.stat().st_mode&0o111))
            changes[key]=dict(before=old,after=sha(path))
    engine=None
    if sail_artifact is not None:
        sys.path.insert(0,str(repo/'install/native'))
        from sail import verify
        wheel,engine=verify(sail_artifact,manifest['target'],root=repo)
        site='python/runtime/lib/python3.12/site-packages/'
        def replace(relative,data,executable=False):
            path=destination/relative
            if not path.resolve().is_relative_to(destination):raise ValueError('unsafe Sail artifact path')
            before=sha(path) if path.exists() else None
            import hashlib
            after=hashlib.sha256(data).hexdigest()
            if before==after:return
            if path.exists():path.unlink()
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o755 if executable else 0o644)
            changes[relative]=dict(before=before,after=after)
            manifest['files'][relative]=dict(sha256=after,executable=executable)
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.is_dir():continue
                if not (item.filename.startswith('pysail/') or item.filename.startswith('pysail-0.7.1.dist-info/')):
                    raise ValueError('unexpected Sail wheel layout')
                replace(site+item.filename,archive.read(item),bool((item.external_attr>>16)&0o111))
        for path in sorted(sail_artifact.rglob('*')):
            if not path.is_file() or path.suffix=='.whl':continue
            name=str(path.relative_to(sail_artifact))
            relative=('licenses/sail/'+name[len('licenses/'):] if name.startswith('licenses/') else 'provenance/sail/'+name)
            replace(relative,path.read_bytes())
        replace('provenance/sail/source.lock.json',(repo/'components/sail-source.lock.json').read_bytes())
        manifest['provenance']['sail']=engine
    delta_engine=None
    if delta_artifact is not None:
        sys.path.insert(0,str(repo/'install/native'))
        from delta_runtime import verify as verify_delta
        wheel,delta_engine=verify_delta(delta_artifact,manifest['target'],root=repo)
        site='python/runtime/lib/python3.12/site-packages/'
        def replace_delta(relative,data,executable=False):
            import hashlib
            path=destination/relative
            if not path.resolve().is_relative_to(destination):raise ValueError('unsafe Delta artifact path')
            before=sha(path) if path.exists() else None;after=hashlib.sha256(data).hexdigest()
            if before==after:return
            if path.exists():path.unlink()
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o755 if executable else 0o644)
            changes[relative]=dict(before=before,after=after)
            manifest['files'][relative]=dict(sha256=after,executable=executable)
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.is_dir():continue
                if not (item.filename.startswith('deltalake/') or item.filename.startswith('deltalake-1.6.3.dist-info/')):
                    raise ValueError('unexpected Delta wheel layout')
                replace_delta(site+item.filename,archive.read(item),bool((item.external_attr>>16)&0o111))
        for path in sorted(delta_artifact.rglob('*')):
            if not path.is_file() or path.suffix=='.whl':continue
            name=str(path.relative_to(delta_artifact))
            relative=('licenses/deltalake/'+name[len('licenses/'):] if name.startswith('licenses/') else 'provenance/deltalake/'+name)
            replace_delta(relative,path.read_bytes())
        replace_delta('provenance/deltalake/source.lock.json',(repo/'components/deltalake-source.lock.json').read_bytes())
        replace_delta('components/components.lock.json',(repo/'components/components.lock.json').read_bytes())
        manifest['provenance']['deltalake']=delta_engine
    p=destination/'release.json';p.unlink();p.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    if sha(base/'release.json')!=identity:raise ValueError('base changed')
    for name,entry in changes.items():
        if (sha(base/name) if (base/name).exists() else None)!=entry['before']:raise ValueError('base payload changed')
    verification=json.loads(subprocess.check_output([str(destination/'bin/supabricks'),'installation','verify'],text=True))
    assert verification['verified'] and verification['identity']==sha(p)
    proof.write_text(json.dumps(dict(scope='Verified source-built native/Python engineering overlay; unsigned' if engine or delta_engine else 'Python-only engineering overlay; unsigned',base_identity=identity,
        identity=sha(p),sail_commit=engine['commit'] if engine else None,delta_commit=delta_engine['commit'] if delta_engine else None,changes=changes,verified_base_files=base_files,verification=verification),indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('base','destination','repo','proof'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--source',action='append',default=[])
    p.add_argument('--sail-artifact',type=Path)
    p.add_argument('--delta-artifact',type=Path)
    a=p.parse_args()
    if not (a.source or a.sail_artifact or a.delta_artifact):p.error('at least one source or native artifact required')
    overlay(a.base.resolve(),a.destination.resolve(),a.repo.resolve(),a.proof.resolve(),a.source,a.sail_artifact.resolve() if a.sail_artifact else None,a.delta_artifact.resolve() if a.delta_artifact else None)
