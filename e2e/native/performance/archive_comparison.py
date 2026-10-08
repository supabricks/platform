#!/usr/bin/env python3
"""Export sanitized structured evidence, excluding scratch and private logs."""
import argparse
import fcntl
import gzip
import json
from pathlib import Path

from compare import comparison_report, sha
from comparison import read_json, require


def archive(source, destination, allow_incomplete=False):
    # Use the controller's lock, including while it is waiting for a quiet host.
    with (source/'.comparison.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return archive_stopped(source, destination, allow_incomplete)


def archive_stopped(source, destination, allow_incomplete):
    record = read_json(source/'experiment.json')
    complete = record['state'] == 'complete'
    require(complete or allow_incomplete, 'incomplete experiment requires explicit diagnostic export')
    if complete:
        comparison_report(source, record)
    destination.mkdir(parents=True, exist_ok=False)
    replacements = {str(source.resolve()): '<experiment>'}
    if record['config'].get('host_continuity'):
        replacements[record['config']['host_continuity']]='<campaign-quiet-checkpoint>'
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
    # A crash can precede the result receipt. Preserve known public formats only,
    # never scratch or private logs. Partial files remain byte-exact diagnostics:
    # parsing or repairing them could hide the very failure being investigated.
    unrecorded = {}
    if not complete:
        for folder in sorted(source.glob('*-attempt*-*')):
            if not folder.is_dir():
                continue
            paths = [folder/'matrix.json', folder/'host-io.json.gz', folder/'observer-controller.json']
            for trial in folder.glob('*-cpu*-rate*-r*'):
                paths.extend(trial/name for name in ('trial.json', 'cleanup.json', 'profile.json.gz'))
            for path in paths:
                relative = str(path.relative_to(source))
                if not path.is_file() or relative in exported:
                    continue
                require(path.resolve().is_relative_to(source.resolve()), 'evidence path escapes root')
                data = path.read_bytes()
                original = sha(path)
                target = destination/relative
                target.parent.mkdir(parents=True, exist_ok=True)
                # JSON reports may contain local package/output paths. Preserve
                # incomplete text too, with the same explicit path redaction.
                target.write_bytes(data if path.suffix == '.gz' else sanitize(data.decode()).encode())
                require(sha(path) == original, 'source changed during export')
                unrecorded[relative] = dict(original_sha256=original, exported_sha256=sha(target))
    for collection in ('attempts', 'pairs'):
        for attempt in record[collection]:
            for result in attempt['results'].values():
                result['original_evidence_sha256'] = result['evidence_sha256']
                result['evidence_sha256'] = {p: exported[str(Path(result['directory'])/p)] for p in result['evidence_sha256']}
    record = sanitize(record)
    record['export'] = dict(original_experiment_sha256=sha(source/'experiment.json'),
                           redaction='Local harness, runtime and experiment paths replaced by stable labels; original artifact hashes retained')
    if not complete:
        record['export'].update(incomplete=True, performance_qualification=False,
            cleanup_established_by_export=False, unrecorded_artifacts=unrecorded)
    (destination/'experiment.json').write_text(json.dumps(record, indent=2, sort_keys=True)+'\n')
    (destination/'host').mkdir()
    for path in sorted((source/'host').glob('*.jsonl')):
        (destination/'host'/(path.name+'.gz')).write_bytes(gzip.compress(path.read_bytes(), mtime=0))
    continuity=source/'host'/'quiet-continuity.json'
    if continuity.exists():
        (destination/'host'/continuity.name).write_text(json.dumps(sanitize(read_json(continuity)),indent=2)+'\n')
    sources = {p.name: p.read_text() for p in Path(__file__).parent.glob('*.py')}
    (destination/'analysis-source.json.gz').write_bytes(gzip.compress(json.dumps(sources,sort_keys=True).encode(),mtime=0))
    if complete:
        comparison_report(destination, read_json(destination/'experiment.json'))
    paths = sorted(p.relative_to(destination) for p in destination.rglob('*') if p.is_file())
    (destination/'SHA256SUMS').write_text(''.join(f'{sha(destination/p)}  {p}\n' for p in paths))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--allow-incomplete', action='store_true',
                        help='preserve a stopped failed experiment as diagnostics without qualifying performance or cleanup')
    args = parser.parse_args()
    archive(args.source, args.destination, args.allow_incomplete)
