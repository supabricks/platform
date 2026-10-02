#!/usr/bin/env python3
"""Matched durable journal component: groups, bounded pruning and a pinned reader."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time


def run(args):
    sys.path.insert(0,str(args.analytics))
    from capture.spool import Spool
    from capture.wal import reader_lease
    samples=[];prunes=0;reclaimed=0;peak=0
    with tempfile.TemporaryDirectory(prefix='sb-journal-',dir=args.scratch) as d:
        root=Path(d)/'spool';identity=dict(generation='component',decoder_version=1)
        spool=Spool(root,identity,64*1024*1024);reader=None;lease=None
        try:
            spool.establish(100,{});end=100
            # The same raw, leased SQLite snapshot runs against both revisions.
            # It intentionally exercises the unchanged cross-process access model.
            lease=reader_lease(spool.path)
            reader=sqlite3.connect(spool.path);reader.execute('BEGIN')
            assert reader.execute('SELECT count(*) FROM transactions').fetchone()==(0,)
            started=time.perf_counter();cpu=time.process_time()
            for group in range(128):
                txs=[]
                for i in range(32):
                    txs.append((end+1,end+2,b'v'*1024));end+=2
                begin=time.perf_counter();result=spool.append_many(txs)
                samples.append((time.perf_counter()-begin)*1000)
                assert result['captured_lsn']==end and result['transactions']==32
                if group%16==15:
                    count=spool.prune(f'{end>>32:X}/{end&0xffffffff:X}')
                    if count:prunes+=1;reclaimed+=count
                if group==63:
                    assert reader.execute('SELECT count(*) FROM transactions').fetchone()==(0,)
                    reader.close();reader=None;os.close(lease);lease=None
                journal=spool.backend.journal if hasattr(spool,'backend') else spool.journal
                peak=max(peak,journal.physical())
            elapsed=time.perf_counter()-started;cpu=time.process_time()-cpu
            spool.verify();captured=spool.captured
            assert captured==end and prunes>=3
            spool.close();spool=Spool(root,identity,64*1024*1024)
            assert spool.captured==captured
            assert spool.append_many(txs)['transactions']==0
            result=dict(status='PASS',groups=128,transactions=4096,payload_bytes=4096*1024,
                elapsed_seconds=elapsed,cpu_seconds=cpu,transactions_per_second=4096/elapsed,
                append_ms=dict(median=statistics.median(samples),p95=sorted(samples)[121],maximum=max(samples)),
                prune_cycles=prunes,reclaimed_payload_bytes=reclaimed,peak_physical_bytes=peak,
                checks=['pinned_snapshot_stable','durable_group_cursor','bounded_pruning','reopen_and_replay'],
                scope='Direct durable capture journal; fixed 1KiB opaque payloads, no PostgreSQL/Delta capacity claim')
        finally:
            if reader:reader.close()
            if lease is not None:os.close(lease)
            spool.close()
    args.report.write_text(json.dumps(result,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--analytics',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    run(p.parse_args())
