#!/usr/bin/env python3
"""SP03a diagnostic overlay: native inspection only, byte-identical sync workers/dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def overlay(repo, base, destination, binary, proof):
    assert not destination.exists()
    assert not subprocess.check_output(['git','-C',str(repo),'status','--porcelain']), 'freeze a clean source tree before packaging'
    original=sha(base/'release.json')
    manifest=json.loads((base/'release.json').read_text())
    assert (base/'python/analytics/profile_io.so').is_file()
    subprocess.run(['cp','-al',str(base),str(destination)],check=True)
    changes={}
    for name,source,executable in [('bin/supabricks',binary,True),
            ('provenance/sqlite-policy.json',repo/'components/sqlite-policy.json',False)]:
        path=destination/name
        before=sha(path) if path.exists() else None
        path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(source.read_bytes());path.chmod(0o755 if executable else 0o644)
        manifest['files'][name]=dict(sha256=sha(path),executable=executable)
        changes[name]=dict(before=before,after=sha(path),bytes=path.stat().st_size)
    path=destination/'release.json';path.unlink()
    path.write_text(json.dumps(manifest,indent=2,sort_keys=True)+'\n')
    assert sha(base/'release.json')==original
    for name,value in changes.items():
        assert (sha(base/name) if (base/name).exists() else None)==value['before']
    unchanged=sum(1 for name in manifest['files'] if name not in changes)
    # Every other manifest entry and physical file remains the original hardlink.
    for name in manifest['files']:
        if name not in changes:
            assert (destination/name).stat().st_ino==(base/name).stat().st_ino
    result=dict(base_release_sha256=original,release_sha256=sha(path),
        source_revision=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        changes=changes,unchanged_files=unchanged,
        native_build=['cargo','build','--locked','--release','-p','supabricks-local','--features','sync-profile','--bin','supabricks'],
        sqlite_dependency_change=False,worker_or_probe_change=False,signed_release=False)
    proof.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('repo','base','destination','binary','proof'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    overlay(*(getattr(args,k).resolve() for k in ('repo','base','destination','binary','proof')))
