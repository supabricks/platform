"""Isolated decode/typed-transfer attribution on an explicit journal clone.

No source mutation or publication. One prepared fixture, identical decoded
objects, unprofiled paired timings followed by a separate cProfile diagnostic.
This excludes process launch, real transport and overlap; installed trials own
throughput and aggregate memory claims.
"""
import argparse
import copy
import cProfile
import hashlib
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import time
import uuid
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('release','config','spool','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();os.umask(0o077)
    sys.path.insert(0,str(args.release.resolve()/'python/analytics'))
    from incremental import lookahead as p
    from incremental.storage import journal
    from capture.spool import atomic,canonical
    config=json.loads(args.config.read_text());config.pop('journal_access',None)
    config.update(spool=str(args.spool.resolve()),deadline_ms=int(time.time()*1000)+120000)
    data=journal(config);stats=config['_journal_read']
    work=args.output.resolve().with_suffix('.work');work.mkdir(mode=0o700)
    prepared=work/'prepared';prepared.mkdir(mode=0o700)
    config.update(workspace=str(work),journal_access={'component':'read-only-clone'},storage_profile='large')
    config.setdefault('attempt',1)
    auth={k:config[k] for k in p.SCOPE+('after_lsn','target_lsn','deadline_ms')}
    auth.update(preparation=1,id=str(uuid.uuid4()),attempt=1,epoch_id=config['previous']['epoch_id'],
                workspace=str(prepared),schema_sha256=hashlib.sha256(canonical(data[0])).hexdigest())
    atomic(prepared/'input.json',auth)
    def read(job):job['_journal_read']=stats;return data
    with patch.object(p,'journal',read):p.prepare(copy.deepcopy(auth))
    config['prepared_batch']=dict(authorization=auth,receipt=json.loads((prepared/'result.json').read_text()))
    expected=p.decode(config,data,lambda:None);samples=[]
    for index in range(4):
        for kind in (('decode','consume') if index%2==0 else ('consume','decode')):
            wall=time.monotonic();cpu=time.process_time()
            actual=p.decode(config,data,lambda:None) if kind=='decode' else p.consume(config)
            elapsed=time.monotonic()-wall;cpu=time.process_time()-cpu
            assert actual==expected
            if index:samples.append(dict(kind=kind,seconds=elapsed,cpu_seconds=cpu))
            del actual
    profiler=cProfile.Profile();profiler.enable();actual=p.consume(config);profiler.disable()
    assert actual==expected
    stats=pstats.Stats(profiler)
    functions=[dict(file=Path(file).name,line=line,function=name,calls=nc,self_seconds=tt,cumulative_seconds=ct)
        for (file,line,name),(cc,nc,tt,ct,callers) in stats.stats.items()]
    functions.sort(key=lambda x:x['self_seconds'],reverse=True)
    report=dict(scope=__doc__,release_identity=hashlib.sha256((args.release/'release.json').read_bytes()).hexdigest(),
        fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),rows=len(expected.operations),end=expected.end,
        batch_sha256=config['prepared_batch']['receipt']['sha256'],samples=samples,
        median_seconds={k:statistics.median(s['seconds'] for s in samples if s['kind']==k) for k in ('decode','consume')},
        profile_scope='Separate instrumented diagnostic; do not add these times to unprofiled timings',profile=functions[:40])
    with args.output.open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')


if __name__=='__main__':main()
