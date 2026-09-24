#!/usr/bin/env python3
"""Build immutable, relocatable bytecode for the incremental worker's imports."""
import hashlib
import importlib
import json
from pathlib import Path
import py_compile
import sys


def compile_sources(root, sources):
    root=Path(root).resolve();count=0;size=0;files={}
    for source in sorted({Path(p).resolve() for p in sources}):
        if not source.is_relative_to(root):raise ValueError('bytecode source outside release')
        cache=Path(py_compile.compile(str(source),dfile=str(source.relative_to(root)),doraise=True,
            optimize=0,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH))
        count+=1;size+=cache.stat().st_size
        files[str(cache.relative_to(root))]=hashlib.sha256(cache.read_bytes()).hexdigest()
    return dict(invalidation='checked-hash',modules=count,bytes=size,files=files)


def verify_inventory(root, report, inventory):
    """Reject caches removed or changed by later packaging stages."""
    root=Path(root).resolve();files=report['files']
    if not files or len(files)!=report['modules']:
        raise ValueError('incomplete sync bytecode report')
    size=0
    for name,expected in files.items():
        path=root/name
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError('sync bytecode missing from final payload')
        if inventory.get(name,{}).get('sha256')!=expected or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('sync bytecode differs from final inventory')
        size+=path.stat().st_size
    if size!=report['bytes']:
        raise ValueError('sync bytecode size differs from final payload')


if __name__=='__main__':
    root=Path(sys.argv[1]).resolve()
    if Path(sys.base_prefix).resolve()!=root/'python/runtime':
        raise ValueError('compile with the bundled interpreter')
    sys.path.insert(0,str(root/'python/analytics'))
    importlib.import_module('incremental_worker')
    # Only the hot import closure is compiled. Spark/notebook packages outside
    # this closure retain the smaller source-only layout. Checked hashes remain
    # valid after archive relocation and never hide a changed source file.
    sources={Path(m.__file__).resolve() for m in list(sys.modules.values())
        if getattr(m,'__file__',None) and str(m.__file__).endswith('.py')}
    print(json.dumps(compile_sources(root,{p for p in sources if p.is_relative_to(root)})))
