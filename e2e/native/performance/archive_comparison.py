#!/usr/bin/env python3
"""Export structured comparison evidence, excluding scratch and private logs."""
import argparse
from pathlib import Path
import shutil

from compare import comparison_report, sha
from comparison import read_json, require


def archive(source, destination):
    record = read_json(source/'experiment.json')
    require(record['state'] == 'complete', 'only completed experiments can be archived')
    comparison_report(source, record)
    destination.mkdir(parents=True, exist_ok=False)
    paths = {Path('experiment.json'), Path('comparison.json'), Path('comparison.md')}
    for attempt in record['attempts']:
        for result in attempt['results'].values():
            for relative, checksum in result['evidence_sha256'].items():
                path = Path(result['directory'])/relative
                require((source/path).resolve().is_relative_to(source.resolve()), 'evidence path escapes root')
                require(sha(source/path) == checksum, 'attempt evidence changed')
                paths.add(path)
    paths.update(p.relative_to(source) for p in (source/'host').glob('*.jsonl'))
    for relative in sorted(paths):
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source/relative, target)
    comparison_report(destination, read_json(destination/'experiment.json'))
    (destination/'SHA256SUMS').write_text(''.join(f'{sha(destination/p)}  {p}\n' for p in sorted(paths)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    archive(args.source, args.destination)
