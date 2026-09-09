#!/usr/bin/env python3
"""Build a separate N01 dependency archive; do not modify a Supabricks release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'install/native'))
from analytics import digest, macho_dependencies


def assemble(args):
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    runtime = destination / 'python/runtime'
    shutil.copytree(args.release / 'python/runtime', runtime)
    shutil.copytree(args.release / 'engine/lib', destination / 'engine/lib')
    shutil.copytree(args.release / 'licenses', destination / 'licenses')
    lock = tomllib.loads((HERE / 'python/uv.lock').read_text())
    base = tomllib.loads((ROOT / 'python/analytics/uv.lock').read_text())
    versions = {p['name']: p['version'] for p in lock['package'] if 'registry' in p['source']}
    prior = {p['name']: p['version'] for p in base['package'] if 'registry' in p['source']}
    assert all(versions.get(k) == v for k, v in prior.items()), 'Existing analytical package changed'
    extra = set(versions) - set(prior)
    baseline_component = destination.parent / 'baseline-python.tar.gz'
    with tarfile.open(baseline_component, 'w:gz', compresslevel=1) as tar:
        tar.add(runtime, arcname='probe/python/runtime')
        tar.add(destination/'engine/lib', arcname='probe/engine/lib')
        tar.add(destination/'licenses', arcname='probe/licenses')
    baseline_compressed_bytes=baseline_component.stat().st_size
    baseline_component.unlink()
    requirements = (HERE / 'python/requirements.lock').read_text()
    blocks = re.split(r'(?=^[a-zA-Z0-9][a-zA-Z0-9_.-]*==)', requirements, flags=re.M)
    selected = ''.join(b for b in blocks if b.split('==')[0] in extra)
    req = destination / 'extra-requirements.lock'
    req.write_text(selected)
    original_files = {p.relative_to(destination) for p in destination.rglob('*') if p.is_file()}
    subprocess.run(['uv','pip','install','--python',str(runtime/'bin/python3.12'),'--no-deps',
        '--require-hashes','--only-binary',':all:','-r',str(req)],check=True)
    worker = destination / 'python/notebooks'
    worker.mkdir()
    for name in ['server.py','kernel.py','bootstrap.py','bridge.py','run.py']:
        shutil.copy2(HERE/name, worker/name)
    shutil.copytree(HERE/'frontend/dist', destination/'assets')
    for cache in runtime.rglob('__pycache__'):
        shutil.rmtree(cache)
    for command in (runtime/'bin').iterdir():
        if command.name != 'python3.12':
            command.unlink()
    # Baseline ELF files already have qualified relative loader paths. Repatching
    # those binary layouts is unnecessary; only added wheel objects need paths.
    loaders = []
    loader_adjustments = []
    allowed = {'libc.so.6','libm.so.6','libpthread.so.0','libdl.so.2','librt.so.1','libutil.so.1','libresolv.so.2'}
    for path in runtime.rglob('*'):
        if not path.is_file(): continue
        with path.open('rb') as stream: magic=stream.read(4)
        if args.target == 'linux-x86_64' and magic == b'\x7fELF':
            if path.relative_to(destination) not in original_files:
                old=subprocess.check_output(['patchelf','--print-rpath',str(path)],text=True).strip()
                paths=[old,'$ORIGIN/'+os.path.relpath(runtime/'lib',path.parent),'$ORIGIN/'+os.path.relpath(destination/'engine/lib',path.parent)]
                subprocess.run(['patchelf','--set-rpath',':'.join(filter(None,paths)),str(path)],check=True)
            listing=subprocess.check_output(['ldd',str(path)],text=True)
            for line in listing.splitlines():
                match=re.search(r'(\S+) => (\S+)',line)
                if 'not found' in line or (match and match[1] not in allowed and not Path(match[2]).resolve().is_relative_to(destination)):
                    raise ValueError('unbundled dependency: '+str(path)+': '+line)
            loaders.append(str(path.relative_to(destination)))
        elif args.target == 'macos-arm64' and magic in (b'\xcf\xfa\xed\xfe',b'\xca\xfe\xba\xbe'):
            listing=subprocess.check_output(['otool','-l',str(path)],text=True)
            # The hash-locked pyzmq 27.2.0 universal2 wheel leaves an unused
            # build RPATH in libsodium; its dependencies use /usr/lib and the
            # other wheel libraries already use @loader_path. Remove only this
            # known RPATH, then restore an ad-hoc signature on the changed file.
            if path.relative_to(runtime).as_posix() == 'lib/python3.12/site-packages/zmq/.dylibs/libsodium.26.dylib' and '/tmp/zmq/lib' in set(macho_dependencies(listing)):
                assert path.relative_to(destination) not in original_files
                assert versions['pyzmq'] == '27.2.0'
                subprocess.run(['install_name_tool','-delete_rpath','/tmp/zmq/lib',str(path)],check=True)
                subprocess.run(['codesign','--force','--sign','-',str(path)],check=True)
                subprocess.run(['codesign','--verify','--strict',str(path)],check=True)
                loader_adjustments.append(dict(path=str(path.relative_to(destination)),removed_rpath='/tmp/zmq/lib',signature='ad-hoc'))
                listing=subprocess.check_output(['otool','-l',str(path)],text=True)
            for dependency in macho_dependencies(listing):
                if dependency.startswith('/') and not dependency.startswith(('/usr/lib/','/System/Library/')):
                    raise ValueError('unbundled dependency: '+dependency)
            loaders.append(str(path.relative_to(destination)))
    wheel_packages = json.loads(subprocess.check_output([str(runtime/'bin/python3.12'),'-I','-B','-c',
        'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"].lower().replace("_","-"):d.version for d in m.distributions()}))'],text=True))
    applicable = json.loads(subprocess.check_output([str(runtime/'bin/python3.12'), '-I', '-B', '-c',
        "import json,sys; from packaging.requirements import Requirement; "
        "lines=open(sys.argv[1]).read().splitlines(); "
        "reqs=[Requirement(line.split(chr(92))[0].strip()) for line in lines if line and line[0].isalnum() and '==' in line]; "
        "print(json.dumps([r.name for r in reqs if r.marker is None or r.marker.evaluate()]))",
        str(HERE/'python/requirements.lock')], text=True))
    selected_versions = {k: versions[k] for k in applicable}
    assert all(wheel_packages.get(k)==v for k,v in selected_versions.items())
    shutil.copy2(HERE/'python/uv.lock', destination/'uv.lock')
    shutil.copy2(HERE/'frontend/package-lock.json',destination/'frontend-package-lock.json')
    frontend_notices={}
    frontend_lock=json.loads((HERE/'frontend/package-lock.json').read_text())
    for relative, package in frontend_lock['packages'].items():
        if not relative: continue
        directory=HERE/'frontend'/relative
        notices=[p for p in directory.glob('*') if p.is_file() and p.name.lower().startswith(('license','copying','notice'))]
        for notice in notices:
            target=destination/'licenses/frontend'/relative.removeprefix('node_modules/')/notice.name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(notice,target)
        frontend_notices[relative]=dict(version=package.get('version'),license=package.get('license'),notices=[p.name for p in notices])
    files = {str(p.relative_to(destination)):digest(p) for p in sorted(destination.rglob('*')) if p.is_file()}
    report = dict(target=args.target, platform_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(), baseline_component_compressed_bytes=baseline_compressed_bytes, frontend_notices=frontend_notices, baseline_release_sha256=digest(args.release/'release.json'),
        analytical_versions_unchanged=prior, packages=selected_versions, added_packages=sorted(extra & set(applicable)),
        loader_check=loaders, loader_adjustments=loader_adjustments, files=files, frontend_bytes=sum(p.stat().st_size for p in (destination/'assets').rglob('*') if p.is_file()))
    (destination/'probe.json').write_text(json.dumps(report,indent=2)+'\n')
    archive = destination.with_suffix('.tar.gz')
    with tarfile.open(archive,'w:gz',compresslevel=1) as tar:
        tar.add(destination,arcname='probe')
    (archive.with_suffix(archive.suffix+'.sha256')).write_text(digest(archive)+'  '+archive.name+'\n')
    size_report=dict(archive_sha256=digest(archive),compressed_bytes=archive.stat().st_size,baseline_component_compressed_bytes=baseline_compressed_bytes,compressed_component_delta_bytes=archive.stat().st_size-baseline_compressed_bytes)
    archive.with_suffix('.size.json').write_text(json.dumps(size_report,indent=2)+'\n')
    print(json.dumps(size_report))


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--target',choices=['linux-x86_64','macos-arm64'],required=True)
    assemble(parser.parse_args())
