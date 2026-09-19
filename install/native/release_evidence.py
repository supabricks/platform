#!/usr/bin/env python3
"""R04: require the complete local workflow on one reviewed archive per target."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re

from environment_evidence import SUITES, collect as environments
from demo import FILES
from sail import verify_report as verify_sail

TARGETS = ('linux-x86_64', 'macos-arm64')
METRICS = ('source_bytes', 'rows', 'decoded_bytes', 'duration_ms', 'peak_rss_bytes',
           'elapsed_seconds', 'archive_bytes', 'unpacked_bytes', 'logical_bytes',
           'allocated_bytes', 'logical_cpus', 'host_memory_bytes', 'cgroup_memory_limit_bytes',
           'duration_seconds', 'cpu_seconds', 'mean_cpu_percent_one_core',
           'source_payload_bytes', 'load_seconds', 'export_payload_mb_per_second')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def passed(data, minimum, label):
    require(data.get('status') == 'passed', f'{label}: failed suite')
    require(not any(data.get(k) for k in ('error', 'errors', 'cleanup', 'cleanup_failed', 'cleanup_errors')),
            f'{label}: errors or incomplete cleanup')
    checks = data.get('checks', [])
    require(isinstance(checks, list) and all(isinstance(c, str) and c for c in checks), f'{label}: invalid checks')
    require(len(set(checks)) >= minimum, f'{label}: incomplete checks')


def metrics(data):
    """Only measured numbers; never copy result payloads, source paths or diagnostics."""
    result = {}
    for key in METRICS:
        if key in data:
            value = data[key]
            require(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0),
                    f'invalid measurement: {key}')
            result[key] = value
    for key in ('disk_before', 'disk_after'):
        if key in data:
            result[key] = metrics(data[key])
    return result


def collect(directory, revision, console, worker, version):
    prior = environments(directory, revision)
    result = dict(status='passed', version=version, revision=revision, console_commit=console, targets={})
    for target in TARGETS:
        env = prior['targets'][target]
        identity = env['release_sha256']
        source = env['source']
        sail = verify_sail(source.get('sail', {}), target)
        frontend = source['console']['source']
        require(sha(identity), 'invalid release manifest identity')
        require(frontend['commit'] == console and frontend['dirty'] is False, 'console source differs from reviewed pin')
        for key in ('manifest_sha256', 'package_lock_sha256'):
            require(sha(source['console'][key]) and frontend.get(key) == source['console'][key], 'console asset provenance mismatch')
        require(source['ingestion']['worker_sha256'] == worker, 'ingestion worker differs from reviewed source')
        require(env['archive']['version'] == version and env['archive']['target'] == target and sha(env['archive']['sha256']),
                'archive version, target or checksum mismatch')
        require(source['data_formats']['local_catalog'] == 10 and source['data_formats']['postgres_major'] == 17,
                'unqualified catalog or PostgreSQL major')
        require(env['release_identity'] == identity, 'lifecycle manifest identity mismatch')
        reports = dict(env['reports'])
        for suite, files in SUITES.items():
            for name, minimum in files.items():
                path = directory / f'{suite}-{target}' / name
                data = json.loads(path.read_text())
                if isinstance(minimum, dict):
                    require(not any(data.get(k) for k in ('error', 'errors', 'cleanup', 'cleanup_failed', 'cleanup_errors')), 'notebook cleanup failed')
                    for nested, count in minimum.items():
                        passed(data[nested], count, f'{suite}/{nested}')
                else:
                    passed(data, minimum, f'{suite}/{name}')

        def read(suite, name, minimum, identity_path):
            path = directory / f'{suite}-{target}' / name
            data = json.loads(path.read_text())
            passed(data, minimum, str(path))
            found = data
            for key in identity_path:
                found = found[key]
            require(found == identity, f'{path}: mixed release identities')
            reports[str(path.relative_to(directory))] = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), checks=len(data['checks']))
            return data

        browser = read('release-console', 'console.json', 39, ('release_sha256',))
        require(browser.get('release_identity') == identity and browser.get('release_version') == version,
                'browser archive differs')
        require(isinstance(browser.get('browser'), str) and re.fullmatch(r'\d+(\.\d+){3}', browser['browser']), 'missing browser version')
        require(browser.get('network_qualification'), 'missing browser network evidence')
        require(set(browser.get('demo', {})) == set(FILES) and all(sha(v) for v in browser['demo'].values()), 'missing installed demo verification')
        ingestion = read('release-ingest', 'ingest.json', 39, ('release', 'identity'))
        require(ingestion['release'].get('verified') is True and ingestion['release'].get('version') == version, 'unverified ingestion release')
        require(ingestion.get('network_evidence'), 'missing ingestion network evidence')
        recovery = read('release-recovery', 'recovery.json', 18, ('release_identity',))
        require(recovery.get('network_qualification'), 'missing recovery network evidence')
        baseline = read('release-qualification', 'qualification.json', 7, ('release_identity',))
        require(baseline.get('network_qualification'), 'missing baseline network evidence')
        benchmark = read('release-qualification', 'benchmarks.json', 7, ('release_identity',)) if target == 'linux-x86_64' else baseline
        for size in (10000000, 100000000, 1000000000):
            for suffix in ('', '_query'):
                measurement = benchmark['measurements'].get(f'snapshot_{size}_bytes{suffix}', {})
                for field in ('elapsed_seconds', 'peak_rss_bytes', 'logical_cpus', 'host_memory_bytes'):
                    require(measurement.get(field, 0) > 0, f'missing snapshot benchmark measurement: {field}')
        network = None
        if target == 'linux-x86_64':
            path = directory / f'release-qualification-{target}/network.json'
            network = json.loads(path.read_text())
            require(network.get('status') == 'passed' and network.get('observed_destinations', 0) > 0
                    and network.get('external_destinations') == [], 'missing or failed network trace')
            reports[str(path.relative_to(directory))] = dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), checks=1)
        require(sha(ingestion['qualification'].get('source_sha256')), 'CSV: missing fixture hash')
        for field in ('source_bytes', 'rows', 'duration_ms', 'peak_rss_bytes'):
            require(ingestion['qualification'].get(field, 0) > 0, f'CSV: missing measurement {field}')
        formats = {}
        for name in ('jsonl', 'json', 'parquet'):
            sample = ingestion['formats'][name]
            budget = sample['budget']
            require(sha(sample['source_sha256']) and sha(budget['source_sha256']), f'{name}: missing fixture hashes')
            for field in ('source_bytes', 'rows', 'duration_ms', 'peak_rss_bytes'):
                require(budget.get(field, 0) > 0, f'{name}: missing measurement {field}')
            measured = metrics(budget)
            measured['worker_source_mib_per_second'] = round(budget['source_bytes'] / 1048576 / (budget['duration_ms'] / 1000), 3)
            formats[name] = dict(source_sha256=sample['source_sha256'], budget_source_sha256=budget['source_sha256'], measurements=measured)
        require(env['notices'] and all(sha(v) for v in env['notices'].values()), 'missing notice inventory')
        result['targets'][target] = dict(
            sail=sail, release_sha256=identity, archive=env['archive'], reports=reports,
            source=dict(platform_commit=revision, console_commit=console,
                        console_manifest_sha256=source['console']['manifest_sha256'],
                        console_lock_sha256=source['console']['package_lock_sha256'], ingestion_worker_sha256=worker),
            browser=dict(engine='Chromium', version=browser['browser'], network=browser['network_qualification']),
            demo=browser['demo'], notices=dict(files=len(env['notices']), inventory_sha256=digest(env['notices'])),
            ingestion=dict(csv_source_sha256=ingestion['qualification']['source_sha256'], csv=metrics(ingestion['qualification']), formats=formats, network=ingestion['network_evidence']),
            baseline=dict(network=baseline['network_qualification'],
                          scope='Daemon and sampled live descendants; excludes harness and CLI. RSS can double-count shared pages and miss short peaks.', measurements={k:metrics(v) for k,v in benchmark['measurements'].items()}),
            network_destinations=network['observed_destinations'] if network else None)
    return result


def markdown(report):
    lines = ['# Qualified local release', '', f"Version: `{report['version']}` · source: `{report['revision']}`", '',
             f"Console source: `{report['console_commit']}`", '',
             f"Sail source: `{report['targets']['linux-x86_64']['sail']['commit']}` (native build on each target)", '',
             '| Target | Chromium | Checks across reports | Manifest SHA-256 |', '| --- | --- | ---: | --- |']
    for target, data in report['targets'].items():
        lines.append(f"| {target} | {data['browser']['version']} | {sum(r['checks'] for r in data['reports'].values())} | `{data['release_sha256']}` |")
    lines += ['', '## Ingestion measurements', '',
              'Synthetic fixtures; throughput covers the worker interval, not upload, inspection or total user latency. RSS is sampled and can miss short peaks. Compressed Parquet source throughput is not decoded throughput.', '',
              '| Target | Format | Source bytes | Rows | Worker seconds | Source MiB/s | Sampled peak MiB |', '| --- | --- | ---: | ---: | ---: | ---: | ---: |']
    for target, data in report['targets'].items():
        samples = {'csv':data['ingestion']['csv'], **{k:v['measurements'] for k,v in data['ingestion']['formats'].items()}}
        for name, m in samples.items():
            seconds=m['duration_ms']/1000
            lines.append(f"| {target} | {name} | {m['source_bytes']} | {m['rows']} | {seconds:.3f} | {m['source_bytes']/1048576/seconds:.3f} | {m['peak_rss_bytes']/1048576:.1f} |")
    lines += ['', 'The JSON companion records fixture, archive, asset, worker, notice and report hashes. These are local engineering archives. Public hosting/signing/notarization, redistribution audit, physical power-loss tests, Safari/Firefox and other OS targets are separate gates.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--console', required=True)
    parser.add_argument('--worker', required=True, type=Path)
    parser.add_argument('--version', default='v0.1.0-alpha.18')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    report = collect(args.directory, args.revision, args.console, hashlib.sha256(args.worker.read_bytes()).hexdigest(), args.version)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2)+'\n')
    args.report.with_suffix('.md').write_text(markdown(report))
    print('R04: complete local workflow qualified for both exact archives')
