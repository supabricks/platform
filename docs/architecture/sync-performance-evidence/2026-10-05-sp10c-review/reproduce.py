#!/usr/bin/env python3
"""Verify exported archives and reproduce this review without original build roots."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

root=Path(__file__).resolve().parent

def semantic(value):
    if isinstance(value,dict):
        return {k:semantic(v) for k,v in value.items() if not k.endswith('_sha256')}
    if isinstance(value,list):return [semantic(v) for v in value]
    return value

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

results={}
with tempfile.TemporaryDirectory(prefix='sp10c-evidence-') as temporary:
    for name in ('observer-bridge','engine-comparison','product-comparison','history-comparison','maintenance-comparison','sustained'):
        target=Path(temporary)/name;target.mkdir()
        archive=root/(name+'.tar.gz')
        with tarfile.open(archive) as stream:
            # Evidence archives contain regular files only; no links or devices.
            for member in stream:
                path=(target/member.name).resolve()
                if not member.isfile() or not path.is_relative_to(target):raise ValueError('unsafe archive entry')
                path.parent.mkdir(parents=True,exist_ok=True)
                with stream.extractfile(member) as source,path.open('wb') as destination:
                    import shutil
                    shutil.copyfileobj(source,destination)
        mode='campaign' if name in ('observer-bridge','engine-comparison','product-comparison') else 'sustained' if name=='sustained' else 'phase'
        output=Path(temporary)/(name+'.json')
        subprocess.run([sys.executable,str(root/'review.py'),mode,str(target),str(output),'--archive'],check=True)
        actual=json.loads(output.read_text());expected=json.loads((root/(name+'.json')).read_text())
        if semantic(actual)!=semantic(expected):raise ValueError('exported metrics differ: '+name)
        results[name]={'archive_sha256':sha(archive),'metrics_match_original_review':True}
print(json.dumps({'status':'all_exported_evidence_reproduced','archives':results},indent=2))
