#!/usr/bin/env python3
"""Build the pinned Delta wheel with the reviewed bounded-merge correction."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def build(source,target,output):
    pin=json.loads((ROOT/'components/deltalake-source.lock.json').read_text())
    actual={('Linux','x86_64'):'linux-x86_64',('Darwin','arm64'):'macos-arm64'}.get((platform.system(),platform.machine()))
    if target!=actual:raise ValueError('Delta requires a native target builder')
    source,output=source.resolve(),output.resolve()
    def git(*args):return subprocess.check_output(['git','-C',str(source),*args],text=True).strip()
    if git('rev-parse','HEAD')!=pin['commit'] or git('status','--porcelain','--untracked-files=normal'):
        raise ValueError('Delta checkout must be clean and match the source pin')
    if {name:sha(source/name) for name in pin['inputs']}!=pin['inputs']:
        raise ValueError('Delta source inputs differ')
    if importlib.metadata.version('maturin')!=pin['maturin']:raise ValueError('incorrect Maturin builder')
    patch=ROOT/pin['patch']['path'];lock=ROOT/pin['cargo_lock']['path']
    if sha(patch)!=pin['patch']['sha256'] or sha(lock)!=pin['cargo_lock']['sha256']:
        raise ValueError('Delta patch or published dependency lock differs')
    for name,hashes in pin['patch']['files'].items():
        if sha(source/name)!=hashes['before']:raise ValueError('Delta patch input differs')
    if output.exists():raise ValueError('fresh artifact output required')
    subprocess.run(['git','-C',str(source),'apply','--check',str(patch)],check=True)
    subprocess.run(['git','-C',str(source),'apply',str(patch)],check=True)
    shutil.copyfile(lock,source/'Cargo.lock')
    def validate_source():
        if set(git('diff','--name-only').splitlines())!=set(pin['patch']['files']):raise ValueError('unexpected Delta source changes')
        if sha(source/'Cargo.lock')!=pin['cargo_lock']['sha256']:raise ValueError('Delta dependency lock changed')
        for name,hashes in pin['patch']['files'].items():
            if sha(source/name)!=hashes['after']:raise ValueError('Delta patched source differs')
        if {name:sha(source/name) for name in pin['inputs']}!=pin['inputs']:raise ValueError('Delta build inputs changed')
    validate_source();output.mkdir(parents=True)
    env={k:v for k,v in os.environ.items() if not k.startswith(('CARGO_PROFILE_','RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS'))}
    env.update(pin['profile'],RUSTUP_TOOLCHAIN=pin['rust'],MACOSX_DEPLOYMENT_TARGET='15.0',SOURCE_DATE_EPOCH=git('show','-s','--format=%ct','HEAD'))
    def run(*args,capture=False):
        r=subprocess.run(list(map(str,args)),cwd=source/'python',env=env,check=True,text=True,stdout=subprocess.PIPE if capture else None)
        return r.stdout.strip() if capture else None
    rustc=run('rustc','--version',capture=True)
    if rustc.split()[1]!=pin['rust']:raise ValueError('incorrect Rust compiler')
    started=time.monotonic()
    run(sys.executable,'-m','maturin','build','--release','--locked','--strip','--compatibility','off','--interpreter',sys.executable,'--out',output)
    wheels=list(output.glob('*.whl'))
    if len(wheels)!=1 or not wheels[0].name.startswith('deltalake-'+pin['version']+'-'):raise ValueError('unexpected Delta wheels')
    validate_source()
    metadata=json.loads(run('cargo','metadata','--locked','--format-version','1',capture=True));packages=[]
    for package in metadata['packages']:
        record={k:package[k] for k in ('name','version','source','license','repository')};record['notices']=[]
        for notice in sorted(Path(package['manifest_path']).parent.iterdir()):
            if notice.is_file() and re.match(r'(?i)^(license|copying|copyright|notice)([._-]|$)',notice.name):
                dest=output/'licenses'/f"{package['name']}-{package['version']}"/notice.name
                dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(notice,dest);record['notices'].append(str(dest.relative_to(output)))
        packages.append(record)
    (output/'dependencies.json').write_text(json.dumps(packages,indent=2)+'\n')
    for name in ('Cargo.lock','LICENSE.txt'):shutil.copyfile(source/name,output/name)
    shutil.copyfile(patch,output/'bounded-merge.patch')
    report=dict(schema_version=1,repository=pin['repository'],commit=pin['commit'],version=pin['version'],target=target,
                inputs=pin['inputs'],patch=pin['patch'],cargo_lock=pin['cargo_lock'],rustc=rustc,maturin=pin['maturin'],profile=pin['profile'],
                builder_script_sha256=sha(Path(__file__)),source_lock_sha256=sha(ROOT/'components/deltalake-source.lock.json'),
                builder=platform.platform(),elapsed_seconds=time.monotonic()-started,
                wheel=dict(file=wheels[0].name,sha256=sha(wheels[0])),
                files={str(p.relative_to(output)):sha(p) for p in sorted(output.rglob('*')) if p.is_file()})
    (output/'deltalake-build.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('commit','target','wheel','elapsed_seconds')}))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','output'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--target',choices=['linux-x86_64','macos-arm64'],required=True)
    a=p.parse_args();build(a.source,a.target,a.output)
