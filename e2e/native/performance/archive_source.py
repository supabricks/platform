#!/usr/bin/env python3
"""Archive all SP06 source attempts, excluding scratch/private logs."""
import argparse
import copy
import fcntl
import gzip
import hashlib
import json
from pathlib import Path


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(source,destination,allow_incomplete=False):
    # The frozen controller holds this lock throughout measurement. Never export
    # a changing live campaign, including while it is waiting for a quiet host.
    with (source/'.controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return archive_stopped(source,destination,allow_incomplete)


def archive_stopped(source,destination,allow_incomplete):
    original=json.loads((source/'experiment.json').read_text())
    assert original['state']=='complete' or allow_incomplete,'incomplete campaign requires explicit diagnostic export'
    destination.mkdir(parents=True,exist_ok=False)
    seen={}
    for attempt in original['attempts']:
        for result in attempt['results']:
            folder=Path(result['directory'])
            for name,digest in result['evidence_sha256'].items():
                relative=folder/name;path=(source/relative).resolve()
                assert path.is_relative_to(source.resolve()) and sha(path)==digest
                out=destination/relative;out.parent.mkdir(parents=True,exist_ok=True)
                out.write_bytes(path.read_bytes());seen[str(relative)]=digest
    # A failed trial can write useful structured evidence before the controller
    # records its result. Preserve only the known public report formats; never
    # traverse scratch or copy private logs/configuration.
    unrecorded={}
    if original['state']!='complete':
        for folder in source.iterdir():
            if not folder.is_dir() or '-attempt' not in folder.name:continue
            for name in ('trial.json','cleanup.json','source-samples.json.gz','profile.json.gz'):
                path=folder/name;relative=str(path.relative_to(source))
                if not path.is_file() or relative in seen:continue
                assert path.resolve().is_relative_to(source.resolve())
                data=path.read_bytes();digest=hashlib.sha256(data).hexdigest()
                out=destination/relative;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(data)
                assert sha(path)==digest,'source changed during export'
                unrecorded[relative]=digest;seen[relative]=digest
    record=copy.deepcopy(original)
    replacements={original['config']['harness']:'<frozen-harness>',original['config']['release']:'<accepted-runtime>',str(source.resolve()):'<experiment>'}
    def redact(value):
        if isinstance(value,str):
            for before,after in sorted(replacements.items(),key=lambda x:len(x[0]),reverse=True):value=value.replace(before,after)
        elif isinstance(value,list):value=[redact(x) for x in value]
        elif isinstance(value,dict):value={k:redact(v) for k,v in value.items()}
        return value
    record=redact(record)
    record['export']=dict(original_experiment_sha256=sha(source/'experiment.json'),redaction='Local controller/package/output paths replaced with stable labels; raw fixed-label structured evidence unchanged')
    if original['state']!='complete':
        record['export'].update(incomplete=True,capacity_qualification=False,
            cleanup_established_by_export=False,unrecorded_artifacts=unrecorded)
    (destination/'experiment.json').write_text(json.dumps(record,indent=2,sort_keys=True)+'\n')
    (destination/'host').mkdir()
    for p in (source/'host').glob('*.jsonl'):(destination/'host'/(p.name+'.gz')).write_bytes(gzip.compress(p.read_bytes(),mtime=0))
    frozen=Path(original['config']['harness'])/'e2e/native/performance'
    sources={p.name:p.read_text() for p in frozen.glob('*.py')}
    (destination/'analysis-source.json.gz').write_bytes(gzip.compress(json.dumps(sources,sort_keys=True).encode(),mtime=0))
    for name,digest in seen.items():assert sha(destination/name)==digest
    files=sorted(p for p in destination.rglob('*') if p.is_file())
    (destination/'SHA256SUMS').write_text(''.join(sha(p)+'  '+str(p.relative_to(destination))+'\n' for p in files))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('destination',type=Path)
    p.add_argument('--allow-incomplete',action='store_true',help='preserve a stopped incomplete campaign as diagnostic evidence, without qualifying capacity or cleanup')
    a=p.parse_args();archive(a.source,a.destination,a.allow_incomplete)
