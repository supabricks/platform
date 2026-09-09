#!/usr/bin/env python3
"""Verify the pinned predecessor, assemble a probe archive, then relocate it."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--directory',type=Path,required=True)
p.add_argument('--output',type=Path,required=True)
p.add_argument('--target',required=True)
a=p.parse_args()
archive=a.directory/f'supabricks-v0.1.0-alpha.8-{a.target}.tar.gz'
expected=Path(str(archive)+'.sha256').read_text().split()[0]
with archive.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==expected
base=a.output/'baseline';base.mkdir(parents=True)
with tarfile.open(archive) as t:t.extractall(base,filter='data')
subprocess.run([str((base/'supabricks/bin/supabricks').resolve()),'installation','verify'],check=True)
probe=a.output/'probe'
subprocess.run([sys.executable,str(Path(__file__).with_name('assemble.py')),'--release',str(base/'supabricks'),'--output',str(probe),'--target',a.target],check=True)
packed=probe.with_suffix('.tar.gz')
shutil.rmtree(probe)
relocated=a.output/'unpacked';relocated.mkdir()
with tarfile.open(packed) as t:t.extractall(relocated,filter='data')
(relocated/'probe').rename(a.output/'relocated probe')
relocated.rmdir()
