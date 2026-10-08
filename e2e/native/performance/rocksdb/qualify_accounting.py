#!/usr/bin/env python3
"""Repeated native retirement/accounting checks; not a throughput benchmark."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import time
import unittest
from unittest.mock import patch

from rocks_journal import RocksJournal
import rocks_journal
from test_contract import RocksTests


def run(repeats,scans,report):
    assert not report.exists(), 'preserve existing qualification evidence'
    started=time.monotonic();original=RocksJournal.sizes;lstat=Path.lstat
    evidence=dict(status='running',repeats=repeats,scans_per_call=scans,completed=0,
        retired_observations=0,retired_examples=[],samples=0,
        backend_sha256=hashlib.sha256(Path(rocks_journal.__file__).read_bytes()).hexdigest(),
        scope='Correctness under repeated native compaction/retirement; no performance claim')
    def observed(path,*args,**kwargs):
        meta=lstat(path,*args,**kwargs)
        if path.parent.name=='rocksdb' and stat.S_ISREG(meta.st_mode) and meta.st_nlink==0:
            evidence['retired_observations']+=1
            if len(evidence['retired_examples'])<16:
                evidence['retired_examples'].append(dict(name=path.name,mode=meta.st_mode,nlink=meta.st_nlink,bytes=meta.st_size))
        return meta
    def repeated(journal):
        for _ in range(scans):
            result=original(journal);evidence['samples']+=1
        return result
    try:
        with patch.object(RocksJournal,'sizes',repeated),patch.object(Path,'lstat',observed):
            for index in range(repeats):
                print('RESOURCE_CYCLE_FIXTURE',index+1,flush=True)
                suite=unittest.TestSuite([RocksTests('test_pinned_view_resource_cycles_release_and_resume')])
                result=unittest.TextTestRunner(verbosity=1).run(suite)
                if not result.wasSuccessful():
                    evidence.update(status='FAIL',failures=len(result.failures),errors=len(result.errors));return False
                evidence['completed']+=1
        evidence['status']='PASS';return True
    finally:
        evidence['elapsed_seconds']=time.monotonic()-started
        report.write_text(json.dumps(evidence,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats',type=int,default=5)
    parser.add_argument('--scans',type=int,default=50)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    if not 1<=args.repeats<=100 or not 1<=args.scans<=100:parser.error('repeats/scans must be 1..100')
    raise SystemExit(0 if run(args.repeats,args.scans,args.report) else 1)
