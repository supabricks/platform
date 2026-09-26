#!/usr/bin/env python3
"""Archive all SP06 source attempts, excluding scratch/private logs."""
import argparse
import copy
import gzip
import hashlib
import json
from pathlib import Path


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(source,destination):
    original=json.loads((source/'experiment.json').read_text());assert original['state']=='complete'
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
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('destination',type=Path);a=p.parse_args();archive(a.source,a.destination)
