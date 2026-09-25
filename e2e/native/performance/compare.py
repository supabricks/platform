#!/usr/bin/env python3
"""Run paired, unchanged-workload sync experiments with immutable identities."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import time

from comparison import compare_records, load_trial, read_json, require
from host_monitor import HostMonitor
from matrix import affinity, cells, output, topology

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CELLS = '4:50,16:50,8:1000,16:1000'


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(1024*1024):
            digest.update(data)
    return digest.hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True)+'\n')
    temporary.replace(path)


def harness_identity(root):
    require(not output('git', '-C', str(root), 'status', '--porcelain'), 'harness must be committed and clean')
    paths = output('git', '-C', str(root), 'ls-files', 'e2e/native', 'install/native').splitlines()
    hashes = {name: sha(root/name) for name in paths if name.endswith('.py')}
    require('e2e/native/performance/matrix.py' in hashes, 'missing benchmark harness')
    return dict(revision=output('git', '-C', str(root), 'rev-parse', 'HEAD'), files=hashes)


def package_identity(release, revision):
    require(len(revision) == 40 and all(c in '0123456789abcdef' for c in revision), 'runtime revision must be a full commit')
    return dict(runtime_revision=revision, release_identity=sha(release/'release.json'),
                binary_sha256=sha(release/'bin/supabricks'))


def pair_order(selected, repeats, seed):
    result = []
    for index, (cpu, rate) in enumerate(selected):
        for repeat in range(1, repeats+1):
            arms = ['predecessor', 'candidate']
            if (repeat+index) % 2:
                arms.reverse()
            result.append(dict(cpus=cpu, rate=rate, repeat=repeat, arms=arms))
    random.Random(seed).shuffle(result)
    return result


def expected(config, arm, pair):
    return dict(config['arms'][arm]['package'], image_id=config['image_id'],
                cpus=pair['cpus'], rate=pair['rate'], affinity=config['affinity'][str(pair['cpus'])],
                memory_gib=config['memory_gib'], harness_identity=config['arms'][arm]['harness_identity'],
                parameters=dict(config['parameters'], rate=pair['rate']))


def verify_identities(config):
    for arm in config['arms'].values():
        require(harness_identity(Path(arm['harness'])) == arm['harness_identity'], 'harness changed during experiment')
        require(package_identity(Path(arm['release']), arm['package']['runtime_revision']) == arm['package'], 'runtime changed during experiment')
    require(output('docker', 'image', 'inspect', config['image_id'], '--format', '{{.Id}}') == config['image_id'], 'qualifier image changed')
    require(topology() == config['topology'], 'CPU topology changed')


def validate_resume(previous, config):
    require(previous['config'] == config, 'resume identities, workload or settings changed')
    require(previous['state'] in ('waiting', 'between_pairs', 'complete'), 'interrupted trial requires cleanup investigation before a new series')
    if previous['state'] == 'complete':
        require(len(previous['pairs']) == len(config['order']), 'completed experiment is missing pairs')
    seen = set()
    for item in previous['pairs']:
        index = item['index']
        require(index not in seen and index < len(config['order']), 'duplicate or unknown pair')
        require(item['pair'] == config['order'][index], 'pair identity differs')
        require(set(item['results']) == {'predecessor', 'candidate'}, 'incomplete accepted pair')
        seen.add(index)
    require(seen == set(range(len(seen))), 'accepted pair sequence has a gap')


def historical_rows(root):
    if root is None:
        return []
    for line in (root/'SHA256SUMS').read_text().splitlines():
        checksum, relative = line.split(maxsplit=1)
        path = (root/relative.strip()).resolve()
        require(path.is_relative_to(root.resolve()), 'archive path escapes root')
        require(sha(path) == checksum, 'historical archive checksum differs')
    followup = read_json(root/'followup.json')
    result = []
    for attempt in followup['attempts']:
        if attempt['kind'] != 'matrix' or not attempt['include_in_quiet_comparison']:
            continue
        directory = root/'matrices'/attempt['name']
        manifest = read_json(directory/'matrix.json')
        e = dict(release_identity=manifest['release_identity'], binary_sha256=manifest['binary_sha256'],
                 runtime_revision=manifest['runtime_revision'], image_id=manifest['image_id'],
                 memory_gib=manifest['memory_gib'], cpus=attempt['cpus'], rate=attempt['rate'],
                 affinity=manifest['affinity'][str(attempt['cpus'])],
                 parameters=dict(profile=True, rate=attempt['rate'], seconds=45, clients=4, rows=10000, baseline=5, warmup=5))
        result.append(dict(cpus=attempt['cpus'], rate=attempt['rate'], repeat=attempt['repeat'], **load_trial(directory, e)))
    require(len(result) == 12 and len({(r['cpus'], r['rate'], r['repeat']) for r in result}) == 12, 'historical baseline is incomplete or duplicated')
    return result


def comparison_report(root, record):
    validate_resume(record, record['config'])
    pairs = []
    for item in record['pairs']:
        pair = item['pair']
        outcomes = {}
        for arm in ('predecessor', 'candidate'):
            receipt = item['results'][arm]
            for path, checksum in receipt['evidence_sha256'].items():
                require(sha(root/receipt['directory']/path) == checksum, 'trial evidence checksum changed')
            outcomes[arm] = load_trial(root/receipt['directory'], expected(record['config'], arm, pair))
            require(outcomes[arm] == receipt['metrics'], 'recorded outcome changed')
        pairs.append(dict(cpus=pair['cpus'], rate=pair['rate'], repeat=pair['repeat'], **outcomes))
    report = compare_records(pairs, record['historical'])
    report.update(slice=record['config']['slice'], hypothesis=record['config']['hypothesis'],
                  complete=record['state'] == 'complete', standard_matched_protocol=record['config'].get('standard_matched_protocol', False), accepted_pairs=len(pairs),
                  attempts=len(record['attempts']), decision='Review required; do not infer speedup from implementation',
                  baseline_identity=record['config'].get('baseline_identity'))
    save(root/'comparison.json', report)
    lines = ['# Paired synchronization comparison', '', record['config']['hypothesis'], '',
             'Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.', '',
             '| CPUs | Offered rows/s | Predecessor complete | Candidate complete | Predecessor p95 median (ms) | Candidate p95 median (ms) |',
             '| --- | --- | --- | --- | --- | --- |']
    for group in report['groups']:
        a, b = group['predecessor'], group['candidate']
        def show(x):
            return 'Unavailable' if x is None else f'{x:.3f}'
        lines.append(f"| {group['cpus']} | {group['rate']} | {a['measured']}/{a['trials']} | {b['measured']}/{b['trials']} | {show(a['metrics']['lag_p95_ms']['median'])} | {show(b['metrics']['lag_p95_ms']['median'])} |")
    (root/'comparison.md').write_text('\n'.join(lines)+'\n')
    return report


def run_trial(config, arm, pair, directory, monitor, quiet):
    definition = config['arms'][arm]
    verify_identities(config)
    command = [sys.executable, str(Path(definition['harness'])/'e2e/native/performance/matrix.py'),
               '--release', definition['release'], '--runtime-revision', definition['package']['runtime_revision'],
               '--output', str(directory), '--cpus', str(pair['cpus']), '--rates', str(pair['rate']),
               '--repeats', '1', '--image', config['image_id'], '--memory-gib', str(config['memory_gib']),
               '--seconds', str(config['parameters']['seconds']), '--clients', str(config['parameters']['clients']),
               '--rows', str(config['parameters']['rows']), '--seed', str(config['seed'])]
    if config['parameters']['profile']:
        command.append('--profile')
    # The predecessor may be the original runner, so invoke its existing one-cell
    # interface. Explicit pairing lives here; neither arm gets a changed workload.
    started = time.time()*1000
    print('TRIAL_START '+json.dumps(dict(arm=arm, pair=pair, directory=directory.name)), flush=True)
    with directory.with_suffix('.private.log').open('w') as log:
        child = subprocess.Popen(command, cwd=definition['harness'], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic()+720
            while child.poll() is None:
                monitor.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('matrix controller exceeded its declared deadline')
                time.sleep(1)
        except BaseException:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            raise  # Never resume automatically after uncertain descendant cleanup.
    ended = time.time()*1000
    # Include the next sampling boundary so a short tail cannot escape contention checks.
    target = time.monotonic()
    while monitor.last_sample <= target:
        monitor.check()
        time.sleep(.1)
    verify_identities(config)
    require(child.returncode == 0, 'trial measurement/cleanup failed; inspect private log')
    metrics = load_trial(directory, expected(config, arm, pair))
    return dict(directory=directory.name, started_at_ms=started, ended_at_ms=ended, quiet=quiet,
                overlap_samples=monitor.overlap(started, time.time()*1000), metrics=metrics,
                evidence_sha256={str(p.relative_to(directory)): sha(p) for p in directory.rglob('*')
                                 if p.is_file() and 'scratch' not in p.relative_to(directory).parts and (p.suffix == '.json' or p.name == 'profile.json.gz')})


def main(args):
    args.output.mkdir(mode=0o700, parents=True, exist_ok=args.resume)
    lock = (args.output/'.comparison.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    selected = args.cells
    groups = topology()
    arms = {}
    for arm in ('predecessor', 'candidate'):
        root = getattr(args, arm+'_harness').resolve()
        release = getattr(args, arm+'_release').resolve()
        arms[arm] = dict(harness=str(root), release=str(release), harness_identity=harness_identity(root),
                         package=package_identity(release, getattr(args, arm+'_revision')))
    config = dict(format_version=1, slice=args.slice, hypothesis=args.hypothesis, arms=arms,
                  image_id=output('docker', 'image', 'inspect', args.image, '--format', '{{.Id}}'),
                  topology=groups, affinity={str(cpu): affinity(groups,cpu) for cpu,rate in selected},
                  host={"lscpu": [r for r in json.loads(output('lscpu','-J'))['lscpu'] if 'MHz' not in r['field']]},
                  filesystem=json.loads(output('findmnt','--json','-T',str(args.output),'-o','SOURCE,FSTYPE,OPTIONS')),
                  memory_gib=args.memory_gib, parameters=dict(profile=not args.no_profile, seconds=args.seconds,
                  clients=args.clients, rows=args.rows, baseline=5, warmup=5),
                  seed=args.seed, order=pair_order(selected, args.repeats, args.seed),
                  quiet_seconds=args.quiet_seconds, sample_interval=5,
                  max_wait_seconds=args.max_wait_seconds, max_pair_attempts=args.max_pair_attempts,
                  baseline_identity=sha(args.baseline/'SHA256SUMS') if args.baseline else None)
    config['standard_matched_protocol'] = (set(selected) == set(cells(DEFAULT_CELLS)) and args.repeats == 3
        and args.memory_gib == 16 and args.seconds == 45 and args.clients == 4 and args.rows == 10000
        and args.quiet_seconds >= 300 and not args.no_profile)
    record = dict(config=config, historical=historical_rows(args.baseline), started_at_ms=time.time()*1000,
                  state='between_pairs', pairs=[], attempts=[])
    manifest = args.output/'experiment.json'
    if args.resume:
        prior = read_json(manifest)
        validate_resume(prior, config)
        require(prior['historical'] == record['historical'], 'historical evidence changed')
        record = prior
        for attempt in record['attempts']:
            if len(attempt['results']) < 2:
                attempt['reason'] = 'interrupted_between_trials; retained and replaced as a whole pair'
        # Re-validate every accepted receipt/profile before continuing or reporting.
        comparison_report(args.output, record)
        if record['state'] == 'complete':
            return
    save(manifest, record)
    monitor = HostMonitor(args.output, quiet_seconds=args.quiet_seconds).start()
    try:
        for index, pair in enumerate(config['order']):
            if index < len(record['pairs']):
                continue
            prior_attempts = sum(a['index'] == index for a in record['attempts'])
            for attempt in range(prior_attempts+1, args.max_pair_attempts+1):
                receipt = dict(index=index, pair=pair, attempt=attempt, results={}, accepted=False)
                record['attempts'].append(receipt)
                for arm in pair['arms']:
                    record['state'] = 'waiting'
                    save(manifest, record)
                    quiet = monitor.wait_quiet(args.max_wait_seconds)
                    directory = args.output/f'{index+1:02}-attempt{len(record["attempts"]):02}-{arm}'
                    record['state'] = 'running_trial'
                    save(manifest, record)
                    result = run_trial(config, arm, pair, directory, monitor, quiet)
                    receipt['results'][arm] = result
                    record['state'] = 'between_pairs'
                    save(manifest, record)
                    print('TRIAL_END '+json.dumps(dict(arm=arm, status=result['metrics']['status'], overlaps=len(result['overlap_samples']))), flush=True)
                receipt['accepted'] = not any(r['overlap_samples'] for r in receipt['results'].values())
                receipt['reason'] = 'quiet_pair' if receipt['accepted'] else 'external_build_overlap'
                if receipt['accepted']:
                    record['pairs'].append(receipt)
                    save(manifest, record)
                    comparison_report(args.output, record)
                    print(f'PAIR_COMPLETE {index+1}/{len(config["order"])}', flush=True)
                    break
                save(manifest, record)
                print('PAIR_CONTENDED: retaining both arms and repeating after quiet interval', flush=True)
            else:
                raise RuntimeError('pair contention replacement limit reached; inspect host evidence')
    finally:
        monitor.close()
    record['state'] = 'complete'
    record['completed_at_ms'] = time.time()*1000
    save(manifest, record)
    comparison_report(args.output, record)
    print('COMPARISON_COMPLETE '+str(manifest), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--slice', required=True)
    parser.add_argument('--hypothesis', required=True)
    for arm in ('predecessor', 'candidate'):
        parser.add_argument('--'+arm+'-release', type=Path, required=True)
        parser.add_argument('--'+arm+'-revision', required=True)
        parser.add_argument('--'+arm+'-harness', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path, help='immutable workflow-profile archive')
    parser.add_argument('--cells', type=cells, default=cells(DEFAULT_CELLS))
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--seed', type=int, default=20260924)
    parser.add_argument('--memory-gib', type=int, default=16)
    parser.add_argument('--seconds', type=int, default=45)
    parser.add_argument('--clients', type=int, default=4)
    parser.add_argument('--rows', type=int, default=10000)
    parser.add_argument('--quiet-seconds', type=int, default=300)
    parser.add_argument('--max-wait-seconds', type=int, default=21600)
    parser.add_argument('--max-pair-attempts', type=int, default=3)
    parser.add_argument('--image', default='supabricks-sy08-qualifier:latest')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--no-profile', action='store_true', help='separately labeled profiler activation control')
    args = parser.parse_args()
    if min(args.repeats,args.memory_gib,args.seconds,args.clients,args.rows,args.quiet_seconds,args.max_wait_seconds,args.max_pair_attempts) < 1 or args.rows < args.clients:
        parser.error('positive dimensions and at least one row/client required')
    main(args)
