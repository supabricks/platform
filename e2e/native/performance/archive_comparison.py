#!/usr/bin/env python3
"""Export sanitized structured evidence, excluding scratch and private logs."""
import argparse
import gzip
import json
from pathlib import Path

from compare import comparison_report, sha
from comparison import read_json, require


def archive(source, destination):
    record = read_json(source/'experiment.json')
    require(record['state'] == 'complete', 'only completed experiments can be archived')
    comparison_report(source, record)
    destination.mkdir(parents=True, exist_ok=False)
    replacements = {str(source.resolve()): '<experiment>'}
    for arm, definition in record['config']['arms'].items():
        replacements[definition['harness']] = '<'+arm+'-harness>'
        replacements[definition['release']] = '<runtime-'+definition['package']['release_identity'][:12]+'>'
    replacements = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)

    def sanitize(value):
        if isinstance(value, str):
            for before, after in replacements:
                value = value.replace(before, after)
            return value
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, dict):
            return {sanitize(k): sanitize(v) for k, v in value.items()}
        return value

    exported = {}
    for attempt in record['attempts']:
        for result in attempt['results'].values():
            for relative, checksum in result['evidence_sha256'].items():
                path = Path(result['directory'])/relative
                require((source/path).resolve().is_relative_to(source.resolve()), 'evidence path escapes root')
                require(sha(source/path) == checksum, 'attempt evidence changed')
                target = destination/path
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix == '.gz':
                    # Worker/profile output already omits private paths and data.
                    target.write_bytes((source/path).read_bytes())
                else:
                    target.write_text(json.dumps(sanitize(read_json(source/path)), indent=2, sort_keys=True)+'\n')
                exported[str(path)] = sha(target)
    for collection in ('attempts', 'pairs'):
        for attempt in record[collection]:
            for result in attempt['results'].values():
                result['original_evidence_sha256'] = result['evidence_sha256']
                result['evidence_sha256'] = {p: exported[str(Path(result['directory'])/p)] for p in result['evidence_sha256']}
    record = sanitize(record)
    record['export'] = dict(original_experiment_sha256=sha(source/'experiment.json'),
                           redaction='Local harness, runtime and experiment paths replaced by stable labels; original artifact hashes retained')
    (destination/'experiment.json').write_text(json.dumps(record, indent=2, sort_keys=True)+'\n')
    (destination/'host').mkdir()
    for path in sorted((source/'host').glob('*.jsonl')):
        (destination/'host'/(path.name+'.gz')).write_bytes(gzip.compress(path.read_bytes(), mtime=0))
    sources = {p.name: p.read_text() for p in Path(__file__).parent.glob('*.py')}
    (destination/'analysis-source.json.gz').write_bytes(gzip.compress(json.dumps(sources,sort_keys=True).encode(),mtime=0))
    comparison_report(destination, read_json(destination/'experiment.json'))
    paths = sorted(p.relative_to(destination) for p in destination.rglob('*') if p.is_file())
    (destination/'SHA256SUMS').write_text(''.join(f'{sha(destination/p)}  {p}\n' for p in paths))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    archive(args.source, args.destination)
