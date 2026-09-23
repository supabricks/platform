#!/usr/bin/env python3
"""Build immutable, relocatable bytecode for the incremental worker's imports."""
import importlib
import json
from pathlib import Path
import py_compile
import sys


def compile_sources(root, sources):
    root=Path(root).resolve();count=0;size=0
    for source in sorted({Path(p).resolve() for p in sources}):
        if not source.is_relative_to(root):raise ValueError('bytecode source outside release')
        cache=Path(py_compile.compile(str(source),dfile=str(source.relative_to(root)),doraise=True,
            optimize=0,invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH))
        count+=1;size+=cache.stat().st_size
    return dict(invalidation='checked-hash',modules=count,bytes=size)


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
