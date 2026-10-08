#!/usr/bin/env python3
"""Bounded Linux TPC-DS generation. Does not load or qualify the product."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import time
from inputs import LOCK, inventory, sha


def scan(path, columns, deadline=None, progress=None):
    rows = 0
    nulls = [0] * len(columns)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for line in stream:
            if rows % 65536 == 0:
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError('generation validation timeout')
                if progress is not None:
                    progress(path.name, rows)
            digest.update(line)
            if not line.endswith(b'|\n'):
                raise ValueError('incomplete generated row: ' + path.name)
            fields = line[:-2].split(b'|')
            if len(fields) != len(columns):
                raise ValueError('field count differs: ' + path.name)
            for i, value in enumerate(fields):
                if not value:
                    nulls[i] += 1
                    if not columns[i]['nullable']:
                        raise ValueError('required value missing: ' + path.name)
            rows += 1
    if not rows:
        raise ValueError('empty generated table: ' + path.name)
    return dict(file=path.name, rows=rows, bytes=path.stat().st_size,
                sha256=digest.hexdigest(), empty_fields_by_column=nulls)


def generation_profile(name, lock):
    if name == 'sf1':
        return lock['generator']['scale'], lock['pilot'], None
    if name != 'sf100':
        raise ValueError('unsupported generation profile')
    path = Path(__file__).with_name('generation-sf100.json')
    profile = json.loads(path.read_text())
    if profile['format_version'] != 1 or profile['scale'] != 100:
        raise ValueError('invalid SF100 generation profile')
    return profile['scale'], profile['bounds'], dict(name=profile['name'], sha256=sha(path))


def run(args):
    lock = json.loads(LOCK.read_text())
    manifest = inventory(lock, args.inputs)
    kit = args.kit.resolve()
    revision = subprocess.check_output(['git', '-C', str(kit), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(kit), 'diff', 'HEAD', '--'], text=True)
    if revision != lock['generator']['commit'] or dirty:
        raise ValueError('generator tracked source differs from pin')
    for name in ['tools/tpcds.sql', 'tools/release.h', 'EULA.txt']:
        if sha(kit / name) != lock['selected_inputs']['generator/' + name]:
            raise ValueError('generator source differs from pin')
    scale, bounds, profile = generation_profile(getattr(args, 'profile', 'sf1'), lock)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output.parent).free < bounds['minimum_free_gib'] * 1024**3:
        raise ValueError('insufficient pilot disk admission')
    args.output.mkdir(exist_ok=False)
    data = args.output / 'data'; data.mkdir()
    binary, distributions = kit / 'tools/dsdgen', kit / 'tools/tpcds.idx'
    # Upstream's PARAM_MAX_LEN is 80; long values can crash ReportError.
    if len(str(data.resolve()).encode()) >= 80:
        raise ValueError('generator output path must be shorter than 80 bytes')
    command = [str(binary), '-SCALE', str(scale),
               '-DIR', str(data.resolve()), '-DISTRIBUTIONS', 'tpcds.idx',
               '-RNGSEED', str(lock['generator']['seed']), '-QUIET', 'Y']
    report = dict(status='FAIL', scope=f'generation-only SF{scale} engineering pilot',
                  started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  input_lock_sha256=sha(LOCK), generator_revision=revision,
                  fixture_sha256=sha(Path(__file__)),
                  binary_sha256=sha(binary), distributions_sha256=sha(distributions),
                  command=command, bounds=bounds, scale=scale, generation_profile=profile,
                  bounds_note='Generator address space and individual files have kernel limits; total bytes/free space are polled every 0.2s and may overshoot; no product qualification')
    child = None
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    last_progress = [0.0]
    def progress(phase, **values):
        if time.monotonic() - last_progress[0] < 10:
            return
        last_progress[0] = time.monotonic()
        value = dict(phase=phase, scale=scale, elapsed_seconds=time.monotonic()-started,
                     updated_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), **values)
        temporary = args.output / 'progress.json.tmp'
        temporary.write_text(json.dumps(value, indent=2) + '\n')
        temporary.replace(args.output / 'progress.json')
    def limits():
        os.sched_setaffinity(0, bounds['cpus'])
        memory = bounds['memory_gib'] * 1024**3
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        size = bounds['maximum_generated_gib'] * 1024**3
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
    try:
        with (args.output / 'generator.log').open('xb') as log:
            child = subprocess.Popen(command, cwd=kit / 'tools', stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=limits)
            while child.poll() is None:
                if time.monotonic() - started > bounds['timeout_seconds']:
                    raise TimeoutError('generation timeout')
                generated_bytes = sum(p.stat().st_size for p in data.iterdir())
                if generated_bytes > bounds['maximum_generated_gib'] * 1024**3:
                    raise ValueError('generated byte budget exceeded')
                if shutil.disk_usage(data).free < bounds.get('minimum_remaining_free_gib', 0) * 1024**3:
                    raise ValueError('generation free-space reserve reached')
                progress('generating', generated_bytes=generated_bytes)
                time.sleep(.2)
            if child.returncode:
                raise ValueError('generator exit ' + str(child.returncode))
        report['generation_seconds'] = time.monotonic() - started
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        report['generator_cpu_seconds'] = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
        report['child_peak_rss_kib'] = after.ru_maxrss
        expected = {t['name'] + '.dat' for t in manifest['tables'] + manifest['metadata_tables']}
        if {p.name for p in data.iterdir()} != expected:
            raise ValueError('generated table inventory differs')
        deadline = time.monotonic() + bounds.get('validation_timeout_seconds', bounds['timeout_seconds'])
        def validation_progress(table, rows):
            progress('validating', table=table, rows_scanned=rows)
        report['tables'] = [scan(data / (t['name'] + '.dat'), t['columns'], deadline, validation_progress) for t in manifest['tables']]
        report['metadata'] = [scan(data / (t['name'] + '.dat'), t['columns'], deadline, validation_progress) for t in manifest['metadata_tables']]
        report['business_rows'] = sum(t['rows'] for t in report['tables'])
        report['generated_bytes'] = sum(t['bytes'] for t in report['tables'] + report['metadata'])
        if report['generated_bytes'] > bounds['maximum_generated_gib'] * 1024**3:
            raise ValueError('final generated byte budget exceeded')
        report['status'] = 'PASS'
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL); child.wait()
        report['total_seconds_including_validation'] = time.monotonic() - started
        (args.output / 'generation.json').write_text(json.dumps(report, indent=2) + '\n')
        last_progress[0] = 0
        progress('complete', status=report['status'], error=report.get('error'))


if __name__ == '__main__':
    def terminate(_signal, _frame):
        raise KeyboardInterrupt('generation interrupted')
    signal.signal(signal.SIGTERM, terminate)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kit', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', choices=['sf1', 'sf100'], default='sf1')
    run(parser.parse_args())
