"""Exact cache identity for a verified, source-built Delta artifact."""
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
INPUTS=('components/deltalake-source.lock.json','components/deltalake-Cargo.lock',
        'components/deltalake-build-requirements.txt','components/build-deltalake.py',
        'components/patches/deltalake-bounded-merge.patch','components/deltalake-cache-key.py',
        'install/native/delta_runtime.py','install/native/analytics.py',
        '.github/actions/deltalake/action.yml')


def key(root,target,image,python):
    if target not in ('linux-x86_64','macos-arm64') or not image or not python:
        raise ValueError('complete native builder identity required')
    inputs={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in INPUTS}
    contract=dict(target=target,image=image,python=python,inputs=inputs)
    return 'delta-artifact-v1-'+target+'-'+hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest()


if __name__=='__main__':
    image=os.environ['ImageOS']+':'+os.environ['ImageVersion']
    print(key(ROOT,sys.argv[1],image,sys.version))
