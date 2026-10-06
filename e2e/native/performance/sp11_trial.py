#!/usr/bin/env python3
"""SP11 steady fixture: preserve transaction timing evidence and screen each window."""
import argparse
import bisect
import gzip
import json
import os
from pathlib import Path
import psutil
import trial
from sustained_journal import Sustained
from sp11_analysis import EVENT, analyze, read_events
from packed_observer import PackedSamples, PackedMarkers


class Steady(Sustained):
    sample_factory=PackedSamples
    marker_map_factory=PackedMarkers

    def resource_details(self):
        root=Path('/sys/fs/cgroup')
        return dict(qualifier_rss_bytes=psutil.Process(os.getpid()).memory_info().rss,
            cgroup_memory_bytes=int((root/'memory.current').read_text()),
            cgroup_memory_stat={k:int(v) for k,v in
                (line.split() for line in (root/'memory.stat').read_text().splitlines())})


def run(args):
    if not args.screen and args.seconds<900:raise ValueError('SP11 steady fixture requires at least 15 minutes')
    original=trial.attribute
    def attribute(samples,observer,runs):
        result=original(samples,observer,runs)
        cuts=[p['end'] for p in observer.publications]
        if len(samples)*EVENT.size>128*1024**2:raise ValueError('transaction timing evidence budget')
        with gzip.open(args.report.with_name('transactions.bin.gz'),'xb') as out:
            for sample in samples:
                p=observer.publications[bisect.bisect_left(cuts,observer.ends[sample['xid']])]
                out.write(EVENT.pack(sample['ack_ms'],p['at']))
        return result
    trial.attribute=attribute
    Steady.resource_output=args.report.with_name('resources.jsonl')
    Steady.reopen_output=args.report.with_name('reopen.json')
    trial.InstalledContinuous=Steady
    try:code=trial.trial(args)
    finally:trial.attribute=original
    if code:return code
    report=json.loads(args.report.read_text())
    resources=[json.loads(line) for line in args.report.with_name('resources.jsonl').read_text().splitlines()]
    result=analyze(report,read_events(args.report.with_name('transactions.bin.gz')),resources,screen=args.screen)
    result['observer_storage']='packed source timings and sparse xid pages; fixed budgets; qualifier RSS remains charged'
    args.report.with_name('windows.json').write_text(json.dumps(result,indent=2)+'\n')
    return 0 if args.screen or result['status']=='passed' else 2


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('release','report','scratch'):p.add_argument('--'+name,type=Path,required=True)
    for name,default in [('rate',1250),('seconds',900),('baseline',5),('warmup',60),('clients',8),('rows',10000)]:
        p.add_argument('--'+name,type=int,default=default)
    p.add_argument('--screen',action='store_true');p.set_defaults(profile=False)
    raise SystemExit(run(p.parse_args()))
