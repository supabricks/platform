"""Fault injection only into the qualification harness's isolated data root.

Never included in an installation. Read catalog records without printing secrets;
only signal PIDs whose recorded birth time still matches the running process.
"""
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
import psutil

root = Path(sys.argv[1]).resolve()
assert root.parent.name.startswith(('sb-n02-', 'sb-n02-release-')) and root.name == 'data'
action, role = sys.argv[2:4]
db = sqlite3.connect(f'file:{root}/state.sqlite3?mode=ro', uri=True)
records = [json.loads(row[0]) for row in db.execute('SELECT record_json FROM native_processes')]
if action == 'diagnostics':
    # Private, bounded failure evidence survives daemon down; never upload it.
    paths = list((root / 'notebook-work').glob('*/server.log')) + list((root / 'notebook-work').glob('last-server-failure.log'))
    classes = set()
    signals = []
    for index, path in enumerate(paths):
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 65536))
            content = stream.read(65536)
            (root.parent / f'private-server-{index}.log').write_bytes(content)
            import re
            classes.update(re.findall(r'(?m)^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):', content.decode(errors='replace')))
            if b'Assertion failed:' in content:
                classes.add('NativeAssertionFailure')
            for line in content.decode(errors='replace').splitlines():
                if line.startswith('SUPABRICKS_SIGNAL '):
                    signals.append(json.loads(line.removeprefix('SUPABRICKS_SIGNAL ')))
    crashes = []
    stops = []
    with (root / 'daemon.log').open('rb') as stream:
        stream.seek(max(0, (root / 'daemon.log').stat().st_size - 65536))
        for line in stream.read(65536).decode(errors='replace').splitlines():
            if line.startswith('SUPABRICKS_SERVER_STOP '):
                stops.append(json.loads(line.removeprefix('SUPABRICKS_SERVER_STOP ')))
    if sys.platform == 'darwin':
        # Only extract termination categories from this fixture's Python reports.
        # Never retain raw OS reports (which include paths and process context).
        for folder in [Path('/Library/Logs/DiagnosticReports'), Path.home() / 'Library/Logs/DiagnosticReports']:
            for path in sorted(folder.glob('*python*.ips'), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
                try:
                    if time.time() - path.stat().st_mtime > 900:
                        continue
                    content = path.read_text()
                    _, end = json.JSONDecoder().raw_decode(content)
                    crash = json.loads(content[end:].strip())
                    if '/sb-c01-release-' not in crash.get('procPath', ''):
                        continue
                    termination = crash.get('termination', {})
                    crashes.append({k: termination.get(k) for k in ['namespace', 'code', 'indicator', 'byProc', 'byPid']})
                except (OSError, ValueError):
                    pass
    print(json.dumps({'server_error_classes': sorted(classes), 'server_signals': signals[-16:], 'daemon_stops': stops[-16:], 'os_terminations': crashes}))
elif action == 'snapshot':
    processes = []
    for record in records:
        try:
            process = psutil.Process(record['pid'])
            members = [process, *process.children(recursive=True)]
        except psutil.NoSuchProcess:
            members = []
        rss = 0
        for process in members:
            try:
                if os.getpgid(process.pid) == record['pid']:
                    rss += process.memory_info().rss
            except (psutil.NoSuchProcess, ProcessLookupError):
                # Exit can race either OS lookup; retain other live members.
                continue
        processes.append({'role': record['role'], 'pid': record['pid'], 'rss': rss})
    active = db.execute("SELECT count(*) FROM analytical_sessions WHERE state NOT IN ('closed','failed')").fetchone()[0]
    print(json.dumps({'processes': processes, 'active_sessions': active}))
elif action == 'corrupt-bootstrap':
    import uuid
    kernel = str(uuid.UUID(role))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        paths = list((root / 'notebook-work').glob('*/kernels/' + kernel + '.context.json'))
        if paths:
            context = json.loads(paths[0].read_text())
            assert not Path(context['ready']).exists()
            context['epoch_id'] = str(uuid.uuid4())
            temporary = paths[0].with_suffix('.inject.tmp')
            temporary.write_text(json.dumps(context))
            temporary.replace(paths[0])
            print('{}')
            break
        time.sleep(.005)
    else:
        raise RuntimeError('bootstrap context not created')
elif action == 'kill-daemon':
    server = next(r for r in records if r['role'].startswith('notebook-server-'))
    process = psutil.Process(server['pid']).parent()
    command = process.cmdline()
    assert 'daemon' in command and '--data-dir' in command
    assert Path(command[command.index('--data-dir') + 1]).resolve() == root
    process.kill()
    print('{}')
elif action == 'kill':
    candidates = [r for r in records if r['role'].startswith(role)]
    assert len(candidates) == 1
    record = candidates[0]
    process = psutil.Process(record['pid'])
    # Token and root are inherited by every owned process; an unrelated reused
    # PID must never receive a test signal. Do not emit this private environment.
    assert process.environ()['SUPABRICKS_PROCESS_TOKEN'] == record['token']
    os.kill(process.pid, signal.SIGKILL)
    print('{}')
else:
    raise ValueError('unknown fixture action')
