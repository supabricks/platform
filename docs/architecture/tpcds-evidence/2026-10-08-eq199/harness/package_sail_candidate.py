#!/usr/bin/env python3
"""Create an unsigned engineering Sail candidate from a verified installed release.

All non-Sail payload bytes are shared unchanged. Use installation upgrade with a
stopped backup to apply this package; never edit the retained cell's runtime file.
"""
import argparse
import json
from pathlib import Path
import subprocess

from inputs import sha
from sail_candidate import replacement_files, validate


def package(base, destination, artifact, proof):
    assert not destination.exists() and not proof.exists()
    before=json.loads((base/'release.json').read_text())
    original=sha(base/'release.json')
    verified=json.loads(subprocess.check_output([str(base/'bin/supabricks'),'installation','verify'],text=True))
    assert verified['verified'] and verified['identity']==original
    replacements,report=replacement_files(artifact,before['target'])
    destination.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['cp','-al',str(base),str(destination)],check=True)
    for name,data in replacements.items():
        path=destination/name
        mode=path.stat().st_mode if path.exists() else 0o644
        if not path.exists() or path.read_bytes()!=data:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.unlink(missing_ok=True)  # Break hardlinks before modifying any payload.
            path.write_bytes(data);path.chmod(mode)
    after=json.loads(json.dumps(before));after['version']=destination.name
    assert after['version']!=before['version']
    after['provenance']['sail']=report
    for name in replacements:
        after['files'][name]=dict(sha256=sha(destination/name),
                                  executable=bool((destination/name).stat().st_mode & 0o111))
    manifest=destination/'release.json';manifest.unlink()
    manifest.write_text(json.dumps(after,indent=2,sort_keys=True)+'\n')
    changes=validate(before,after,destination,artifact)
    assert sha(base/'release.json')==original
    for name in changes['changed_payload_files']:
        if name in before['files']:
            assert sha(base/name)==before['files'][name]['sha256'], 'base modified'
    verified=json.loads(subprocess.check_output([str(destination/'bin/supabricks'),'installation','verify'],text=True))
    assert verified['verified'] and verified['identity']==sha(manifest)
    proof.write_text(json.dumps(dict(base_release_identity=original,candidate_release_identity=sha(manifest),
                                    signed_release=False,installation_verification=verified,**changes),indent=2)+'\n')
    print(json.dumps(dict(identity=sha(manifest),changed_files=len(changes['changed_payload_files']))))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base','destination','artifact','proof'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    package(*(getattr(args,name).resolve() for name in ('base','destination','artifact','proof')))
