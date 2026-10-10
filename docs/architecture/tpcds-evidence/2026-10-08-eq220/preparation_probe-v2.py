"""Read-only verification/planning probe on explicit private storage clones.

The journal read is outside timing. Both versions use the same mutation lease
and warm checksum evidence. This excludes initialization, apply and publication;
it cannot substitute for the installed prefix qualification.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import resource
import sys
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('release','config','root','spool','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--serial',action='store_true',help='Measure the retained synchronous boundary')
    args=parser.parse_args()
    sys.path.insert(0,str(args.release.resolve()/'python/analytics'))
    import incremental_worker as worker
    from incremental.planning import mutation_lease
    from incremental.storage import journal,verified_digests
    config=json.loads(args.config.read_text())
    config.pop('journal_access',None)
    config.update(spool=str(args.spool.resolve()),deadline_ms=int(time.time()*1000)+120000)
    data=journal(config);previous=config['previous'];samples=[];hashes=[]
    preparation=None if args.serial else getattr(worker,'Preparation',None)
    for index in range(4):
        start=time.monotonic()
        context=preparation(config,data) if preparation else nullcontext(data)
        with context as prepared,mutation_lease(args.root) as lease,verified_digests(lease,config):
            worker.verify_previous(args.root,previous)
            plan=worker.plan(config,args.root,previous,prepared,lease)
        seconds=time.monotonic()-start
        digest=hashlib.sha256(worker.canonical(plan)).hexdigest();hashes.append(digest)
        if index:samples.append(seconds)
    assert len(set(hashes))==1
    highwater=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)
    report=dict(scope=__doc__,release_identity=hashlib.sha256((args.release/'release.json').read_bytes()).hexdigest(),
                fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                preparation_thread=preparation is not None,seconds=samples,plan_sha256=hashes[0],
                end_lsn=plan['end_lsn'],rows=sum(len(t['rows']) for t in plan['tables']),peak_process_rss_bytes=highwater)
    with args.output.open('x') as stream:json.dump(report,stream,indent=2);stream.write('\n')


if __name__=='__main__':main()
