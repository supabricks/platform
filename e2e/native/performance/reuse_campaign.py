#!/usr/bin/env python3
"""Frozen SP09a campaign: lifecycle gates, instrumentation bridge, matched trials."""
import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import traceback

from compare import ROOT, harness_identity, package_identity
from host_monitor import HostMonitor
from matrix import affinity, topology


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2)+'\n')
    temporary.replace(path)


def run(config_path, root):
    config = json.loads(config_path.read_text())
    root.mkdir(parents=True, exist_ok=True)
    lock = (root/'.campaign.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert not (root/'status.json').exists(), 'use a new campaign; never reset partial evidence'
    state = dict(status='preparing', completed=[], config=config,
                 driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 controller_identity=harness_identity(ROOT), started_at_ms=time.time()*1000)

    def checkpoint(**updates):
        state.update(updates)
        save(root/'status.json', state)

    def verify():
        assert harness_identity(ROOT) == state['controller_identity']
        assert shutil.disk_usage(root).free >= 64*1024**3, '64 GiB admission floor'
        for arm in config['arms'].values():
            assert package_identity(Path(arm['release']), arm['revision']) == arm['identity']

    def execute(name, command):
        verify()
        checkpoint(status='running', phase=name)
        print('PHASE_START', name, flush=True)
        with (root/(name+'.log')).open('x') as log:
            return subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT).returncode

    def phase(name, left, right, cells, clients, seconds, warmup, activation=False):
        command = [sys.executable, str(ROOT/'e2e/native/performance/compare.py'),
                   '--slice', 'SP09a-'+name, '--hypothesis', config['hypothesis'],
                   '--output', str(root/name), '--cells', cells, '--clients', str(clients),
                   '--seconds', str(seconds), '--warmup-seconds', str(warmup),
                   '--repeats', '3', '--quiet-seconds', '300', '--minimum-free-gib', '64',
                   '--image', config['image']]
        for label, key in [('predecessor', left), ('candidate', right)]:
            arm = config['arms'][key]
            command += ['--'+label+'-release', arm['release'],
                        '--'+label+'-revision', arm['revision'],
                        '--'+label+'-harness', config['workload_harness']]
        if activation:
            command += ['--activation-control']
        code = execute(name, command)
        archive = [sys.executable, str(ROOT/'e2e/native/performance/archive_comparison.py'),
                   str(root/name), str(root/'archives'/name)]
        if (root/name/'experiment.json').exists():
            subprocess.run(archive + (['--allow-incomplete'] if code else []), check=True)
        assert code == 0, 'measurement/admission/cleanup failure: '+name
        record = json.loads((root/name/'experiment.json').read_text())
        assert record['state'] == 'complete'
        state['completed'].append(name)
        checkpoint(status='between_phases')
        print('PHASE_COMPLETE', name, flush=True)
        return record

    def component():
        directory = root/'component'
        directory.mkdir()
        monitor = HostMonitor(directory).start()
        receipts = []
        try:
            # Predeclared balanced fresh-fixture pairs, with bounded replacements.
            for repeat in range(3):
                arms = ['common', 'candidate'] if repeat % 2 == 0 else ['candidate', 'common']
                for attempt in range(3):
                    pair = []
                    for arm in arms:
                        checkpoint(status='waiting', phase='component')
                        quiet = monitor.wait_quiet()
                        output = directory/f'{repeat+1}-{attempt+1}-{arm}'
                        output.mkdir()
                        name = 'sp09a-component-'+str(__import__('os').getpid())
                        command = ['docker', 'run', '--rm', '--init', '--name', name,
                                   '--network', 'none', '--cpuset-cpus', ','.join(map(str, affinity(topology(), 8))),
                                   '--memory', '16g', '--memory-swap', '16g',
                                   '--user', str(__import__('os').getuid())+':'+str(__import__('os').getgid()),
                                   '-v', str(ROOT)+':/repo:ro',
                                   '-v', config['arms'][arm]['release']+':/release:ro',
                                   '-v', str(output)+':/reports', '-w', '/repo', config['image'],
                                   'python3', 'install/native/catalog_gate.py', '--timeout', '900',
                                   '--report', '/reports/cleanup.json', '--', 'python3',
                                   'e2e/native/performance/reuse_component.py', '--binary', '/release/bin/supabricks',
                                   '--bundle', '/release/engine', '--helpers', '/release/helpers',
                                   '--python', '/release/python/analytics/python',
                                   '--worker', '/release/python/analytics/export.py', '--report', '/reports/component.json']
                        started = time.time()*1000
                        code = execute('component-'+output.name, command)
                        ended = time.time()*1000
                        receipt = dict(arm=arm, repeat=repeat+1, attempt=attempt+1, quiet=quiet,
                                       started_at_ms=started, ended_at_ms=ended, directory=output.name,
                                       overlaps=monitor.overlap(started, ended), exit_code=code,
                                       sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                                               for p in output.glob('*.json')})
                        pair.append(receipt)
                        receipts.append(receipt)
                        save(directory/'receipts.json', receipts)
                        assert code == 0, 'component lifecycle/cleanup failure'
                        cleanup = json.loads((output/'cleanup.json').read_text())
                        assert cleanup['exit_code'] == cleanup['remaining_descendants'] == cleanup['leaked_descendants'] == 0
                        assert not cleanup['timed_out']
                        assert json.loads((output/'component.json').read_text())['status'] == 'PASS'
                    accepted = not any(r['overlaps'] for r in pair)
                    for receipt in pair:
                        receipt['accepted'] = accepted
                    save(directory/'receipts.json', receipts)
                    if accepted:
                        break
                else:
                    raise RuntimeError('component contention replacement limit reached')
        finally:
            monitor.close()
        state['completed'].append('component')
        checkpoint(status='between_phases')

    try:
        verify()
        # Exercise the actual installation parser before any expensive fixture.
        # Payload hashing alone does not establish release-schema compatibility.
        installations = {}
        for key, arm in config['arms'].items():
            result = json.loads(subprocess.check_output(
                [str(Path(arm['release'])/'bin/supabricks'), 'installation', 'verify'], text=True))
            assert result['verified'] and result['identity'] == arm['identity']['release_identity']
            installations[key] = result
        save(root/'installation-preflight.json', installations)
        component()
        bridge = phase('common-refactor-controls', 'accepted', 'common', '8:1250', 8, 300, 60)
        # Equivalence screen flags material drift; never discard unfavorable runs.
        ratios = {key: [] for key in ('source_rows_s', 'lag_p95_ms', 'cpu_cores', 'peak_memory_bytes')}
        for pair in bridge['pairs']:
            metrics = {arm: row['metrics'] for arm, row in pair['results'].items()}
            assert all(m['status'] == 'measured' and m['within_5s_p95'] and m['offered_load_met']
                       for m in metrics.values()), 'bridge runtime/input/freshness outcome requires review'
            for key in ratios:
                ratios[key].append(metrics['candidate'][key]/metrics['predecessor'][key])
        median = {key: statistics.median(values) for key, values in ratios.items()}
        save(root/'bridge-screen.json', dict(ratios=ratios, medians=median))
        assert median['source_rows_s'] >= .95 and median['lag_p95_ms'] <= 1.05 \
            and median['cpu_cores'] <= 1.10 and median['peak_memory_bytes'] <= 1.10, 'bridge drift requires review'
        phase('common-profiler-controls', 'common', 'common', '8:1250', 8, 300, 60, True)
        phase('historical-main', 'common', 'candidate', '4:50,16:50,8:1000,16:1000', 4, 45, 5)
        phase('qualified-main', 'common', 'candidate', '8:1250,16:1250', 8, 300, 60)
        phase('historical-profiler-controls', 'candidate', 'candidate', '4:50,16:50,8:1000,16:1000', 4, 45, 5, True)
        phase('qualified-profiler-controls', 'candidate', 'candidate', '8:1250,16:1250', 8, 300, 60, True)
        checkpoint(status='measurements_complete_review_required', phase='review', completed_at_ms=time.time()*1000)
    except BaseException as error:
        checkpoint(status='stopped_for_investigation', error_type=type(error).__name__, error=str(error))
        traceback.print_exc()
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    run(args.config.resolve(), args.output.resolve())
