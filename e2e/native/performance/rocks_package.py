#!/usr/bin/env python3
"""Frozen experiment overlay: SP10b owner unchanged, explicit RocksDB-only arm."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import zipfile
from native_package import sha


def overlay(repo,base,destination,proof,wheel,rocks=True):
    started=time.perf_counter()
    assert not destination.exists() and not proof.exists()
    import tomllib
    lock=tomllib.loads((repo/'e2e/native/performance/rocksdb/uv.lock').read_text())
    allowed={w['hash'][7:]:w for p in lock['package'] if p['name']=='rocksdict' for w in p['wheels']}
    wheel_sha=sha(wheel);assert wheel_sha in allowed
    assert 'manylinux_2_28_x86_64' in allowed[wheel_sha]['url']
    original=sha(base/'release.json');manifest=json.loads((base/'release.json').read_text());changes={}
    for name,entry in manifest['files'].items():assert sha(base/name)==entry['sha256'],name
    subprocess.run(['cp','-al',str(base),str(destination)],check=True)
    def replace(name,data):
        path=destination/name;before=sha(path) if path.exists() else None
        executable=manifest['files'].get(name,{}).get('executable',False)
        if path.exists():path.unlink()
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);path.chmod(0o755 if executable else 0o644)
        manifest['files'][name]=dict(sha256=sha(path),executable=executable)
        changes[name]=dict(before=before,after=sha(path))
    # Keep every SP10b implementation byte, including the IPC transport. The sole
    # selector is an auditable package-only alias; production has no new setting.
    sources={'capture/rocks_journal.py':(repo/'e2e/native/performance/rocks_journal.py').read_bytes(),
             'capture/sqlite_journal.py':(base/'python/analytics/capture/sqlite_journal.py').read_bytes()+
                b'\n# SP10c frozen experimental package only.\nfrom .rocks_journal import RocksJournal as SQLiteJournal\n'}
    if not rocks:sources={}
    sources['marker_profile.py']=(repo/'e2e/native/performance/marker_profile.py').read_bytes()
    capture=(base/'python/analytics/capture_worker.py').read_text()
    marker="worker_profile.install(globals(), 'capture')"
    assert capture.count(marker)==1
    sources['capture_worker.py']=capture.replace(marker,"import marker_profile\nmarker_profile.install(globals())\n"+marker).encode()
    for name,data in sources.items():
        relative='python/analytics/'+name;replace(relative,data)
        path=destination/relative;cache=path.parent/'__pycache__'/(path.stem+'.cpython-312.pyc')
        before=sha(cache) if cache.exists() else None
        if cache.exists():cache.unlink()
        subprocess.run([str(destination/'python/analytics/python'),'-B','-c',
            'import py_compile,sys;py_compile.compile(sys.argv[1],dfile=sys.argv[2],doraise=True,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)',str(path),relative],check=True)
        name=str(cache.relative_to(destination));manifest['files'][name]=dict(sha256=sha(cache),executable=False)
        changes[name]=dict(before=before,after=sha(cache))
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist() if rocks else []:
            if name.endswith('/'):continue
            assert name.startswith(('rocksdict/','rocksdict-0.3.29.dist-info/')) and '..' not in Path(name).parts
            replace('python/runtime/lib/python3.12/site-packages/'+name,archive.read(name))
    if rocks:
        # Declare the extra leaf dependency in both installed environment locks.
        # Runtime environment verification remains exact; no missing/extra-package
        # exception is introduced to make the experimental wheel importable.
        experiment=(repo/'e2e/native/performance/rocksdb/uv.lock').read_text()
        entry=next('[[package]]'+part for part in experiment.split('[[package]]')[1:] if '\nname = "rocksdict"\n' in part)
        for folder in ('analytics','notebooks'):
            name='python/'+folder+'/uv.lock'
            value=(base/name).read_text()
            assert 'name = "rocksdict"' not in value
            replace(name,(value+'\n'+entry+'\n').encode())
            name='python/'+folder+'/requirements.lock'
            value=(base/name).read_text()
            replace(name,(value+'\nrocksdict==0.3.29 --hash=sha256:'+wheel_sha+'\n').encode())
    replace('python/analytics/sp10c-experiment.json',json.dumps(dict(wheel_sha256=wheel_sha,wheel=allowed[wheel_sha],
        source_sha256=sha(repo/'e2e/native/performance/rocks_journal.py'),rocks=rocks,adopted=False),indent=2).encode())
    path=destination/'release.json';path.unlink();path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    assert sha(base/'release.json')==original
    for name,entry in changes.items():assert (sha(base/name) if (base/name).exists() else None)==entry['before']
    verification=json.loads(subprocess.check_output([str(destination/'bin/supabricks'),'installation','verify'],text=True))
    assert verification['verified'] and verification['identity']==sha(path)
    result=dict(base_release_sha256=original,release_sha256=sha(path),wheel_sha256=wheel_sha,
        revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        changes=changes,installation_verification=verification,signed_release=False,adopted=False,rocks=rocks,
        overlay_seconds=time.perf_counter()-started,wheel_bytes=wheel.stat().st_size,
        added_file_bytes=sum((destination/n).stat().st_size for n,v in changes.items() if v['before'] is None),
        native_rebuild_required=False,source_build_time_measured=False)
    proof.write_text(json.dumps(result,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','base','destination','proof','wheel'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--sqlite',action='store_true')
    a=p.parse_args();overlay(*[getattr(a,n).resolve() for n in ('repo','base','destination','proof','wheel')],rocks=not a.sqlite)
