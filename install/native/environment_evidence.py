#!/usr/bin/env python3
"""Reject missing, failed or mixed-revision NE06 release evidence."""
import argparse
import hashlib
import json
from pathlib import Path


SUITES = {
    'release-environments': {'qualification.json': 10, 'qualification-kernels.json': 10},
    'release-packages': {'qualification.json': 12},
    'release-environment-lifecycle': {'qualification.json': 7, 'qualification-index.json': 6},
    'release-environment-console': {'environments.json': 16},
    'release-notebooks': {'notebooks.json': 13},
    'release-console': {'console.json': 1},
}


def collect(directory, revision):
    result = dict(status='passed', revision=revision, targets={})
    for target in ('linux-x86_64', 'macos-arm64'):
        lifecycle = json.loads((directory / f'release-environment-lifecycle-{target}/qualification.json').read_text())
        assert lifecycle['target'] == target
        assert lifecycle['source']['platform_commit'] == revision
        assert lifecycle['source']['platform_dirty'] is False
        assert lifecycle['archives']['new']['target'] == target
        identity = lifecycle['release_sha256']
        reports = {}
        for suite, files in SUITES.items():
            for name, minimum in files.items():
                path = directory / f'{suite}-{target}' / name
                data = json.loads(path.read_text())
                assert data['status'] == 'passed', f'{path}: failed suite'
                assert not data.get('cleanup_failed') and not data.get('errors'), f'{path}: cleanup or browser errors'
                assert data['release_sha256'] == identity, f'{path}: mixed release identities'
                assert len(data['checks']) >= minimum, f'{path}: incomplete checks'
                reports[str(path.relative_to(directory))] = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), checks=len(data['checks']))
        result['targets'][target] = dict(release_sha256=identity, release_identity=lifecycle['release_identity'],
            archive=lifecycle['archives']['new'], previous=lifecycle['archives']['old'],
            python_version=lifecycle['python_version'], kernel_contract_sha256=lifecycle['kernel_contract_sha256'],
            source=lifecycle['source'], wheels=lifecycle['wheels'], notices=lifecycle['notices'],
            measurements=lifecycle['measurements'], project_bundle=lifecycle['project_bundle'], reports=reports)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.directory, args.revision)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print('NE06: both targets passed all environment suites with matching final release identities')
