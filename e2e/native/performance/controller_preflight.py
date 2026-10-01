#!/usr/bin/env python3
"""Check the host interpreter before starting the frozen SP06 controller."""
import argparse
import gzip
import hashlib
import importlib
import importlib.metadata
import json
from pathlib import Path
import sys


def check(harness,requirements,trial=None):
    versions={}
    for line in requirements.read_text().splitlines():
        if not line or line.startswith('#'):continue
        name,expected=line.split('==')
        actual=importlib.metadata.version(name)
        if actual!=expected:raise ValueError(f'{name}: expected {expected}, found {actual}')
        versions[name]=actual
    sys.path.insert(0,str(harness/'e2e/native/performance'))
    modules={name:importlib.import_module(name) for name in ('source_trial','source_capacity','compare')}
    for name,module in modules.items():
        if not Path(module.__file__).resolve().is_relative_to(harness):raise ValueError('wrong harness module: '+name)
    identity=modules['compare'].harness_identity(harness)
    receipt=dict(status='passed',python=sys.version,dependencies=versions,
        requirements_sha256=hashlib.sha256(requirements.read_bytes()).hexdigest(),
        harness_revision=identity['revision'],checks=['pinned_host_dependencies','frozen_host_imports','clean_harness'],qualification=False)
    if trial:
        report=json.loads((trial/'trial.json').read_text())
        cleanup=json.loads((trial/'cleanup.json').read_text())
        assert report['status']=='measured' and cleanup['exit_code']==0
        assert not cleanup['timed_out'] and cleanup['leaked_descendants']==cleanup['remaining_descendants']==0
        path=trial/'source-samples.json.gz'
        assert hashlib.sha256(path.read_bytes()).hexdigest()==report['samples']['sha256']
        samples=json.loads(gzip.decompress(path.read_bytes()))['samples']
        source=report['source']
        computed=modules['source_trial'].summarize(samples,source['measurement_seconds'],source['elapsed_seconds'])
        assert all(source[k]==value for k,value in computed.items())
        receipt.update(trial=trial.name,source_samples_sha256=report['samples']['sha256'])
        receipt['checks']+=['real_numeric_acknowledgment_recomputation','clean_trial_teardown']
    return receipt


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--harness',type=Path,required=True)
    p.add_argument('--requirements',type=Path,default=Path(__file__).with_name('controller-requirements.txt'))
    p.add_argument('--trial',type=Path)
    p.add_argument('--report',type=Path,required=True)
    args=p.parse_args()
    receipt=check(args.harness.resolve(),args.requirements,args.trial)
    args.report.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))
