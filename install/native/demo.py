"""Validate the shipped, offline user walkthrough against the installed inventory."""
import hashlib
import json
from pathlib import Path

FILES = ('DEMO.md', 'examples/console/sales.csv', 'examples/console/sales.ipynb', 'PROJECT-PORTABILITY.md')
SALES = b'id,amount\n1,10\n2,20\n'
SQL = 'SELECT sum(amount) AS total FROM public.sales'


def verify(release: Path):
    manifest = json.loads((release / 'release.json').read_text())
    hashes = {}
    for name in FILES:
        path = release / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'missing or unsafe installed demo file: {name}')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest['files'].get(name, {}).get('sha256') != hashes[name]:
            raise ValueError(f'installed demo inventory mismatch: {name}')
    if (release / FILES[1]).read_bytes() != SALES:
        raise ValueError('packaged CSV differs from qualified browser demonstration')
    notebook = json.loads((release / FILES[2]).read_text())
    code = [c for c in notebook['cells'] if c['cell_type'] == 'code']
    if notebook['nbformat'] != 4 or len(code) != 1 or code[0].get('outputs') != [] or code[0].get('execution_count') is not None or SQL not in ''.join(code[0]['source']):
        raise ValueError('packaged notebook must contain the qualified query without saved execution')
    return hashes
