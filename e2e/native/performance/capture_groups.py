#!/usr/bin/env python3
"""Durable spool component screen. Synthetic complete two-table transactions, no source capacity claim."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import threading
import time


def payload(commit,end):
    parts=[b'B'+struct.pack('!QqI',commit,0,end//100)]
    for oid in (42,43):
        parts.append(b'I'+struct.pack('!I',oid)+b'N'+struct.pack('!H',2)+b't'+struct.pack('!I',1)+b'1t'+struct.pack('!I',1)+b'2')
    parts.append(b'C'+struct.pack('!BQQq',0,commit,end,0))
    return b''.join(struct.pack('!I',len(p))+p for p in parts)


def qualify(args):
    sys.path.insert(0,str(args.analytics))
    from capture.spool import Spool
    try:from capture.groups import Groups
    except ImportError:Groups=None
    native=None if args.no_profile else ctypes.CDLL(None).sb_profile_io_snapshot
    if native:native.argtypes=[ctypes.POINTER(ctypes.c_uint64)]
    def snapshot():
        values=(ctypes.c_uint64*8)()
        if native:native(values)
        return list(values)
    with tempfile.TemporaryDirectory(prefix='sp02-component-',dir=args.scratch) as tmp:
        spool=Spool(Path(tmp)/'spool',dict(generation='component',decoder_version=2),**({'journal_mode':args.journal_mode} if args.journal_mode else {}));spool.establish(100,{})
        groups=Groups(spool,count=args.count,age=args.age_ms/1000) if Groups and not args.single else None
        stop=threading.Event();reader_errors=[];reader_stats=dict(snapshots=0,busy=0)
        def reader():
            db=sqlite3.connect(f'file:{spool.path}?mode=ro',uri=True,timeout=.05,isolation_level=None)
            try:
                while not stop.is_set():
                    try:
                        db.execute('BEGIN')
                        rows=db.execute('SELECT end_lsn,payload,sha256 FROM transactions ORDER BY seq DESC LIMIT 32').fetchall()
                        for end,raw,digest in rows:
                            assert hashlib.sha256(raw).hexdigest()==digest
                        stop.wait(.002);db.execute('COMMIT');reader_stats['snapshots']+=1
                    except sqlite3.OperationalError as e:
                        if db.in_transaction:db.execute('ROLLBACK')
                        if e.sqlite_errorcode!=sqlite3.SQLITE_BUSY:raise
                        reader_stats['busy']+=1
                    stop.wait(.048)
            except BaseException as e:reader_errors.append(type(e).__name__)
            finally:db.close()
        thread=threading.Thread(target=reader)
        if args.reader:thread.start()
        before=snapshot();start=time.monotonic();cpu=time.process_time();n=0;pruned=0;commits=0;next_prune=256;group_sizes=[]
        def flush():
            nonlocal commits
            result=groups.flush()
            if result and result['transactions']:commits+=1;group_sizes.append(result['transactions'])
        try:
            while time.monotonic()-start<args.seconds:
                due=start+n/args.rate if args.rate else 0
                now=time.monotonic()
                if due>now:
                    wait=min(due-now,groups.wait() if groups else .2)
                    if wait:time.sleep(wait)
                    if groups and groups.wait()==0:flush()
                    continue
                end=200+n*100;tx=(end-20,end,payload(end-20,end))
                if groups:
                    if groups.before(tx):flush()
                    if groups.add(tx):flush()
                else:spool.append(*tx);commits+=1;group_sizes.append(1)
                n+=1
                if n>=next_prune:
                    if groups:flush()
                    pruned+=spool.prune(f'{spool.captured>>32:X}/{spool.captured&0xffffffff:X}');next_prune+=256
            if groups:flush()
            elapsed=time.monotonic()-start;cpu=time.process_time()-cpu;after=snapshot()
        finally:
            stop.set()
            if args.reader:thread.join(timeout=2);assert not thread.is_alive()
        assert not reader_errors
        captured=spool.captured;stats=groups.stats if groups else None
        journal=spool.journal.progress() if hasattr(spool,'journal') else None
        mode=spool.db.execute('PRAGMA journal_mode').fetchone()[0];spool.close()
        # Independent stopped-file check, including every retained payload and
        # the pruned-prefix count. No active measurement observer is needed.
        with sqlite3.connect(f'file:{Path(tmp)/"spool/spool.sqlite3"}?mode=ro',uri=True) as db:
            metadata={k:json.loads(v) for k,v in db.execute('SELECT key,value FROM metadata')}
            previous=(metadata.get('pruned_prefix') or {}).get('lsn',100);pruned_count=(previous-100)//100
            retained=0;size=0
            for end,prior,commit,raw,digest in db.execute('SELECT end_lsn,previous_lsn,commit_lsn,payload,sha256 FROM transactions ORDER BY seq'):
                end,prior,commit=int(end,16),int(prior,16),int(commit,16)
                assert prior==previous and end==previous+100 and commit==end-20
                assert raw==payload(commit,end) and hashlib.sha256(raw).hexdigest()==digest
                previous=end;retained+=1;size+=len(raw)
            assert retained+pruned_count==n and previous==captured==metadata['captured'] and size==metadata['bytes']
            assert db.execute('PRAGMA journal_mode').fetchone()[0]==mode
        calls=(after[0]+after[4])-(before[0]+before[4]);sync_ns=(after[1]+after[5])-(before[1]+before[5])
        assert after[3]+after[7]==before[3]+before[7]
        return dict(status='pass',parameters=dict(count=args.count,age_ms=args.age_ms,offered_transactions_s=args.rate,seconds=args.seconds,reader=args.reader,single=args.single,journal_mode=mode,profile=not args.no_profile),
                    transactions=n,seconds=elapsed,transactions_s=n/elapsed,cpu_seconds=cpu,groups=commits,
                    transactions_per_group=n/commits,group_sizes=group_sizes,stats=stats,reader=reader_stats,
                    sync_calls=calls,syncs_per_transaction=calls/n,sync_ms_per_transaction=sync_ns/n/1e6,
                    pruned_bytes=pruned,correctness='all retained payloads and chain plus pruned-prefix count match generated transactions',
                    journal=journal,durability=mode.upper()+' / FULL; native sync totals include pruning/checkpoints',sqlite_version=sqlite3.sqlite_version)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--analytics',type=Path,required=True);p.add_argument('--scratch',type=Path,required=True)
    p.add_argument('--count',type=int,default=32);p.add_argument('--age-ms',type=int,default=10)
    p.add_argument('--rate',type=float,default=0,help='transactions/s; zero means saturated input')
    p.add_argument('--seconds',type=float,default=10);p.add_argument('--reader',action='store_true');p.add_argument('--single',action='store_true')
    p.add_argument('--journal-mode',choices=('delete','wal'));p.add_argument('--no-profile',action='store_true')
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=qualify(a);a.output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k not in ('group_sizes','stats')}))
