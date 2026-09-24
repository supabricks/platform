#!/usr/bin/env python3
"""PK07: native closures -> one transferable artifact -> clean installed consumers.

Runs in the release's Python. Reports are inputs to R04, never a second release
completion authority. Private logs/data are retained only on failure.
"""
import argparse
import errno
from functools import partial
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

TARGETS = ('linux-x86_64', 'macos-arm64')
CHECKS = (
    'same_artifact_verified_unbound', 'malformed_archive_no_publication',
    'explicit_deployment_sql_spark_notebook', 'two_deployments_conflicting_names_isolated',
    'two_worktrees_changed_binding_stale_plan', 'relocation_requires_attach',
    'missing_wheels_no_activation', 'cancel_retains_resources',
    'interrupted_apply_reconciles', 'restart_retains_revision',
    'signed_upgrade_reprepare', 'cold_restore_reprepare', 'low_disk_no_publication',
    'final_inventory_verified', 'logical_data_transferred_transactionally',
)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def extract(directory, version, target, destination):
    archive = directory / f'supabricks-{version}-{target}.tar.gz'
    sha = digest(archive)
    assert sha == Path(str(archive) + '.sha256').read_text().split()[0]
    with tarfile.open(archive) as tar:
        tar.extractall(destination, filter='data')
    release = destination / 'supabricks'
    subprocess.run([str(release / 'bin/supabricks'), 'installation', 'verify'], check=True, stdout=subprocess.DEVNULL)
    return release, dict(version=version, target=target, sha256=sha)


class Cell:
    def __init__(self, root, release):
        self.root, self.release = root, release
        self.binary = release / 'bin/supabricks'
        self.data = root / 'data'
        self.roots = [self.data]
        home = root / 'home'; home.mkdir(mode=0o700)
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(('PYTHON', 'UV_', 'PIP_', 'JUPYTER', 'IPYTHON', 'PG', 'AWS_', 'SUPABRICKS_')) and not k.upper().endswith('_PROXY')}
        self.env.update(HOME=str(home), PATH='/usr/bin:/bin:/usr/sbin:/sbin', PYTHONDONTWRITEBYTECODE='1')

    def cli(self, at, *parts, success=True):
        scope = [] if parts[:2] == ('installation', 'verify') else ['--data-dir', str(self.data)]
        if parts[:2] not in (('project', 'unpack'), ('project', 'verify'), ('installation', 'verify')) and parts[:3] != ('project', 'data', 'verify'):
            scope += ['--project', str(at)]
        result = subprocess.run([str(self.binary), *map(str, parts), *scope], env=self.env,
                                capture_output=True, text=True, timeout=300)
        (self.root / 'private-command.log').write_text(result.stdout + result.stderr)
        assert (result.returncode == 0) == success, f'CLI {parts[:2]} failed; see private-command.log'
        return json.loads(result.stdout) if success else result

    def plan(self, project):
        path = self.root / 'plan.json'
        path.write_text(json.dumps(self.cli(project, 'project', 'plan')))
        return path

    def settle(self, project, operation, state='succeeded'):
        deadline = time.monotonic() + 300
        while operation['state'] in ('queued', 'preparing', 'activating'):
            assert time.monotonic() < deadline, 'project apply timeout'
            time.sleep(.2)
            operation = self.cli(project, 'project', 'status', operation['id'])
        (self.root / 'private-operation.json').write_text(json.dumps(operation))
        if state is not None:
            assert operation['state'] == state, f'apply expected {state}; see private-operation.json'
        return operation

    def apply(self, project, key, state='succeeded'):
        plan = self.plan(project)
        operation = self.settle(project, self.cli(project, 'project', 'apply', plan, '--key', key), state)
        assert self.cli(project, 'project', 'apply', plan, '--key', key)['id'] == operation['id']
        return operation

    def close(self):
        failed = False
        for data in self.roots:
            if (data / 'runtime.json').exists():
                try:
                    config = json.loads((data / 'runtime.json').read_text())
                    binary = Path(config['bundle']).parent / 'bin/supabricks'
                    result = subprocess.run([str(binary), 'down', '--data-dir', str(data)], env=self.env, capture_output=True, timeout=120)
                    failed |= result.returncode != 0
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    failed = True
        if failed:
            raise RuntimeError('owned cell shutdown failed')


def seed_predecessor(cell, project):
    """Recover only alpha.22's known premature database failure, once.

    The candidate must succeed without this accommodation. Retained resources
    reconcile normally before a new explicit apply; no database is recreated.
    """
    operation = cell.apply(project, 'predecessor', state=None)
    if operation['state'] == 'succeeded':
        return False
    assert operation['state'] == 'failed' and 'database preparation failed or was superseded' in (operation.get('error') or ''), 'unexpected predecessor apply failure'
    # Alpha.22 plans the notebook environment before the database. Its completed
    # environment receipt is legitimate here; migrations, fixtures and activated
    # resources are not. Validate the durable step boundary, not just a key set.
    steps, position = operation['plan']['steps'], operation['next_step']
    assert type(position) is int and 0 <= position < len(steps)
    step = steps[position]
    assert (step['logical'], step['kind'], step['action']) == ('database.main', 'database', 'create'), 'predecessor failure occurred beyond database preparation'
    completed = steps[:position]
    assert len(completed) <= 1 and all(
        (s['logical'], s['kind'], s['action'], s['environment']) ==
        ('environment.notebook', 'environment', 'prepare_offline', 'notebook')
        for s in completed), 'unexpected predecessor preparation order'
    resources = operation['resources']
    expected = {s['logical'] for s in completed}
    assert expected <= set(resources) <= expected | {'database.main'}, 'unexpected predecessor resource receipt'
    for prior in completed:
        resource = resources[prior['logical']]
        assert resource['kind'] == 'environment' and resource['environment'] == 'notebook'
        assert resource['origin'] == operation['id']
        assert not any(resource.get(k) for k in ('branch', 'file', 'database', 'receipt'))
        uuid.UUID(resource['generation'])
    assert cell.cli(project, 'project', 'installed')['active_revision'] is None
    branches = cell.cli(project, 'database', 'list')['branches']
    assert len(branches) == 1
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        branch = cell.cli(project, 'branch', 'get', 'main')
        if branch['revision'] == branch['observed_revision']:
            break
        time.sleep(.2)
    else:
        raise TimeoutError('predecessor retained database did not reconcile')
    cell.cli(project, 'branch', 'resume', 'main', '--wait')
    cell.apply(project, 'predecessor-reconciled')
    assert len(cell.cli(project, 'database', 'list')['branches']) == 1
    return True


def bundle(args, root, release, archive):
    cell = Cell(root, release)
    try:
        project = root / 'exporter'; project.mkdir()
        cell.cli(project, 'init', 'pk07-exporter'); cell.cli(project, 'up')
        cell.cli(project, 'env', 'init', '--wait')
        args.output.mkdir(parents=True, exist_ok=True)
        path = args.output / 'environment.zip'
        cell.cli(project, 'env', 'export-bundle', path, '--offline', '--wait')
        with zipfile.ZipFile(path) as z:
            manifest = json.loads(z.read('bundle.json'))
        report = dict(status='passed', target=args.target, archive=archive,
                      release_sha256=digest(release / 'release.json'), bundle_sha256=digest(path), manifest=manifest)
        if args.target == 'linux-x86_64':
            cell.cli(project, 'database', 'create', 'main', '--key', 'logical-source', '--wait')
            cell.cli(project, 'sql', '--branch', 'main', '--write', '--sql', 'CREATE TABLE public.portable_sales(id integer PRIMARY KEY, amount numeric(18,2), payload bytea)')
            cell.cli(project, 'sql', '--branch', 'main', '--write', '--sql', "INSERT INTO public.portable_sales VALUES(1,10,decode('00ff','hex')),(2,20,NULL)")
            selection=root/'data-tables.json'
            selection.write_text(json.dumps(dict(version=1,tables=[dict(schema='public',name='portable_sales')])))
            logical=cell.cli(project, 'project', 'data', 'export', '--branch', 'main', '--tables', selection, '--output', args.output/'sales.sbdata')
            assert logical['archive_sha256'] == digest(args.output/'sales.sbdata')
            report['logical_data'] = logical
    finally:
        cell.close()
    (args.output / 'bundle.json').write_text(json.dumps(report, indent=2)+'\n')


def produce(args, root, release, archive):
    cell = Cell(root, release)
    source = root / 'source'
    shutil.copytree(release / 'examples/projects/sales-runnable', source)
    (source / 'dependencies').mkdir()
    closures = {}
    pair = None
    for target in TARGETS:
        directory = args.bundles / f'project-bundle-{target}'
        path = directory / 'environment.zip'
        report = json.loads((directory / 'bundle.json').read_text())
        assert report['status'] == 'passed' and report['target'] == target and report['bundle_sha256'] == digest(path)
        with zipfile.ZipFile(path) as z:
            declarations = {n:z.read(n) for n in ('pyproject.toml', 'uv.lock')}
            assert json.loads(z.read('bundle.json')) == report['manifest']
        assert pair is None or pair == declarations, 'native closures have different declarations'
        pair = declarations
        shutil.copy2(path, source / 'dependencies' / f'{target}.zip')
        closures[target] = report
    for name, content in pair.items():
        (source / 'notebooks/environment' / name).write_bytes(content)
    manifest = source / 'supabricks.toml'
    manifest.write_text(manifest.read_text().replace('"migrations/*.sql"]', '"migrations/*.sql", "dependencies/*.zip"]') +
                        '\n[environments.notebook.bundles]\n' + ''.join(f'{t} = "dependencies/{t}.zip"\n' for t in TARGETS))
    args.output.mkdir(parents=True, exist_ok=True)
    data_source=args.bundles/'project-bundle-linux-x86_64/sales.sbdata'
    assert digest(data_source) == closures['linux-x86_64']['logical_data']['archive_sha256']
    shutil.copy2(data_source,args.output/'sales.sbdata')
    package = args.output / 'sales.sbproj'
    report = cell.cli(source, 'project', 'pack', '--output', package)
    # Repacking on this producer must be deterministic. Consumers never repack it.
    repeat = root / 'repeat.sbproj'
    cell.cli(source, 'project', 'pack', '--output', repeat)
    assert digest(repeat) == digest(package)
    with tarfile.open(package) as tar:
        expanded_bytes = sum(member.size for member in tar.getmembers())
    info = dict(status='passed', producer_target=args.target, producer_archive=archive,
                release_sha256=digest(release / 'release.json'), package_sha256=digest(package),
                content_sha256=report['content_sha256'], source_sha256=report['inspection']['source_sha256'],
                environment=report['inspection']['environments']['notebook'], closures=closures,
                logical_data=closures['linux-x86_64']['logical_data'],
                fixtures={name:entry['sha256'] for name, entry in report['inspection']['files'].items()
                          if Path(name).suffix in ('.csv', '.sql', '.ipynb')},
                measurements=dict(archive_bytes=package.stat().st_size,
                                  unpacked_bytes=expanded_bytes, rows=2))
    assert not cell.data.exists(), 'producer unexpectedly started a daemon'
    (args.output / 'producer.json').write_text(json.dumps(info, indent=2)+'\n')


def consume(args, root, release, archive):
    import psutil
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'e2e/native/notebook-environments'))
    from client import Console, execute
    from qualify import Handler
    from stage import stage
    package = args.package / 'sales.sbproj'
    producer = json.loads((args.package / 'producer.json').read_text())
    assert digest(package) == producer['package_sha256']
    report = dict(status='running', target=args.target, archive=archive, checks=[],
                  release_sha256=digest(release / 'release.json'), producer=producer,
                  network_evidence=os.environ.get('SUPABRICKS_PROJECT_NETWORK_EVIDENCE', 'local development; not isolated'),
                  measurements=dict(producer['measurements']))
    cell = Cell(root, release)
    project = root / 'destination'
    channels = []
    server = None
    stop = threading.Event()
    peak = dict(peak_rss_bytes=0, disk_peak_bytes=0)
    sample_errors = []

    def sample():
        while not stop.is_set():
            try:
                rss = 0
                for p in psutil.process_iter(['cmdline']):
                    command = p.info['cmdline'] or []
                    if 'daemon' in command and any(str(d) in command for d in cell.roots):
                        try:
                            rss += sum(c.memory_info().rss for c in [p, *p.children(recursive=True)])
                        except psutil.Error:
                            pass
                peak['peak_rss_bytes'] = max(peak['peak_rss_bytes'], rss)
                allocated = 0
                for d in cell.roots:
                    for parent, _, names in os.walk(d):
                        for name in names:
                            try:
                                allocated += (Path(parent) / name).lstat().st_blocks * 512
                            except FileNotFoundError:
                                pass
                peak['disk_peak_bytes'] = max(peak['disk_peak_bytes'], allocated)
            except Exception as e:
                sample_errors.append(type(e).__name__)
                return
            stop.wait(.5)

    sampler = threading.Thread(target=sample, daemon=True); sampler.start()
    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2)+'\n')
    def check(name):
        assert name in CHECKS and name not in report['checks']
        report['checks'].append(name); save(); print(name, flush=True)
    def unpack(path):
        return cell.cli(project, 'project', 'unpack', package, '--destination', path)
    def sql(at, query):
        return cell.cli(at, 'sql', '--branch', 'main', '--sql', query)['rows']
    def totals(at):
        assert sql(at, 'SELECT count(*),sum(amount) FROM public.sales') == [['2', '30']]
        assert sql(at, 'SELECT count(*) FROM public.project_marker') == [['1']]
    def notebook(at, measure=False):
        installed = cell.cli(at, 'project', 'installed')
        assert not installed['preparation_needed']
        worktree = Path(installed['environment_worktrees']['environment.notebook'])
        generation = installed['resources']['environment.notebook']['generation']
        c = Console(worktree, cell.cli, channels)
        start = time.monotonic(); execution = c.start(generation)
        if measure: report['measurements']['start_seconds'] = time.monotonic()-start
        ws = c.connect(execution)
        try:
            doc = json.loads((worktree / 'notebooks/sales.ipynb').read_text())
            for entry in doc['cells']:
                if entry['cell_type'] == 'code': execute(ws, ''.join(entry['source']))
            execute(ws, "assert spark.table('public.sales').count()==2\nassert spark.sql('SELECT sum(amount) FROM public.sales').first()[0]==30\nassert supabricks_environment['id']=="+repr(generation))
            assert execution['epoch_id'] and execution['environment']['contract'] and execution['environment']['inputs']
        finally:
            ws.close(); c.stop(execution)
    def install(channel, upgrade=False):
        env = dict(cell.env, SUPABRICKS_INSTALL_DIR=str(prefix), SUPABRICKS_DATA_DIR=str(cell.data), SUPABRICKS_NO_MODIFY_PATH='1')
        if upgrade: env.update(SUPABRICKS_UPGRADE='1', SUPABRICKS_BACKUP_DIR=str(root / 'upgrade-backup'))
        curl = subprocess.Popen(['curl', '-fsSL', base+'/'+channel+'/install.sh'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            result = subprocess.run(['bash'], stdin=curl.stdout, env=env, capture_output=True, text=True, timeout=600)
        finally:
            curl.stdout.close()
        curl.wait(timeout=15)
        (root / 'private-installer.log').write_text(result.stdout+result.stderr)
        assert curl.returncode == result.returncode == 0, 'signed installer failed'
        cell.binary = prefix / 'bin/supabricks'

    try:
        prefix = root / "programs ' é"
        web = root / 'web'; web.mkdir()
        key = root / 'preview.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(key)], check=True, capture_output=True)
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(web)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        for channel, directory, version in [('old', args.previous_directory, args.previous_version), ('new', args.directory, args.version)]:
            destination = web / channel; destination.mkdir()
            source = directory / f'supabricks-{version}-{args.target}.tar.gz'
            assert digest(source) == Path(str(source)+'.sha256').read_text().split()[0]
            for path in (source, Path(str(source)+'.sha256')):
                # Staging only reads these immutable archives. Avoid another full
                # archive copy on small runners; cross-device roots still work.
                try: os.link(path, destination / path.name)
                except OSError as error:
                    if error.errno != errno.EXDEV: raise
                    shutil.copy2(path, destination / path.name)
            stage(destination, version, base+'/'+channel, key)
            if channel == 'old': report['previous_archive'] = dict(version=version, target=args.target, sha256=digest(source))
        install('new')
        assert cell.cli(root, 'installation', 'verify')['identity'] == report['release_sha256']
        verified = cell.cli(root, 'project', 'verify', package)
        assert verified['archive_sha256'] == producer['package_sha256']
        assert verified['content_sha256'] == producer['content_sha256']
        assert verified['inspection']['source_sha256'] == producer['source_sha256']
        assert verified['inspection']['environments']['notebook'] == producer['environment']
        unpack(project)
        assert all(digest(project / name) == expected for name, expected in producer['fixtures'].items())
        assert not cell.data.exists()
        cell.cli(project, 'project', 'binding', success=False)
        check('same_artifact_verified_unbound')
        corrupt = root / 'bad.sbproj'; corrupt.write_bytes(b'not a gzip archive')
        bad = root / 'bad-destination'
        cell.cli(root, 'project', 'unpack', corrupt, '--destination', bad, success=False)
        assert not bad.exists()
        cell.cli(root, 'project', 'unpack', package, '--destination', project, success=False)
        check('malformed_archive_no_publication')
        # The shared artifact is first deployed into a clean candidate installation.
        cell.cli(project, 'up')
        binding = cell.cli(project, 'project', 'create', '--key', 'destination')
        start = time.monotonic(); first = cell.apply(project, 'first')
        report['measurements']['prepare_seconds'] = time.monotonic()-start
        totals(project); notebook(project, measure=True)
        session = cell.cli(project, 'analytics', 'open', '--branch', 'main', '--wait')
        assert cell.cli(project, 'analytics', 'sql', '--session', session['id'], '--sql', 'SELECT sum(amount) FROM public.sales')['rows'] == [['30']]
        cell.cli(project, 'analytics', 'close', session['id'], '--wait')
        check('explicit_deployment_sql_spark_notebook')
        second = root / 'second'; unpack(second)
        two = cell.cli(second, 'project', 'create', '--key', 'second')
        assert two['definition_id'] == binding['definition_id'] and two['deployment_id'] != binding['deployment_id']
        cell.apply(second, 'second'); totals(second)
        cell.cli(second, 'sql', '--branch', 'main', '--write', '--sql', 'CREATE TABLE public.only_second(id int)')
        assert sql(project, "SELECT to_regclass('public.only_second')") == [[None]]
        check('two_deployments_conflicting_names_isolated')
        third = root / 'third'; unpack(third)
        cell.cli(third, 'project', 'attach', binding['deployment_id'])
        plan = cell.plan(third)
        cell.cli(third, 'project', 'attach', two['deployment_id'], success=False)
        assert cell.cli(third, 'project', 'binding')['deployment_id'] == binding['deployment_id']
        cell.cli(second, 'project', 'apply', plan, '--key', 'stale-binding', success=False)
        assert cell.cli(project, 'project', 'installed')['active_revision'] == first['id']
        check('two_worktrees_changed_binding_stale_plan')
        moved = root / "moved project ' é"; project.rename(moved); project = moved
        cell.cli(project, 'project', 'binding', success=False)
        cell.cli(project, 'project', 'attach', binding['deployment_id']); totals(project)
        check('relocation_requires_attach')
        cell.cli(project, 'down'); cell.cli(project, 'up')
        assert cell.cli(project, 'project', 'installed')['active_revision'] == first['id']
        totals(project); notebook(project)
        check('restart_retains_revision')
        # Both native targets import these exact Linux-produced bytes. No source
        # roles, branch IDs, OIDs or credentials become destination authority.
        logical=args.package/'sales.sbdata'
        verified=cell.cli(project,'project','data','verify',logical)
        assert verified == producer['logical_data'] and digest(logical)==verified['archive_sha256']
        cell.cli(project,'database','create','transferred','--key','logical-target','--wait')
        imported=cell.cli(project,'project','data','import',logical,'--branch','transferred','--key','portable-data')
        assert imported['state']=='committed' and imported['archive_sha256']==verified['archive_sha256']
        assert imported['destination_branch_id']!=verified['source']['branch_id']
        assert cell.cli(project,'sql','--branch','transferred','--sql',"SELECT count(*),sum(amount),min(encode(payload,'hex')) FROM public.portable_sales")['rows']==[['2','30.00','00ff']]
        assert cell.cli(project,'project','data','import',logical,'--branch','transferred','--key','portable-data')['replayed']
        report['logical_data'] = dict(archive_sha256=verified['archive_sha256'],content_sha256=verified['content_sha256'],rows=2)
        check('logical_data_transferred_transactionally')
        # A predecessor's kernel contract can differ (including built wheel bytes).
        # Seed its installed template, then upgrade and explicitly adopt the same
        # transferred candidate package; never forge/reseal a predecessor closure.
        cell.close()
        # Retire the completed, stopped first fixture before the independent
        # upgrade installation; keep its measured peak, not duplicate releases.
        for finished in (prefix, cell.data, project, second, third):
            shutil.rmtree(finished)
        cell.roots.clear()
        cell.data = root / 'upgrade-data'; cell.roots.append(cell.data)
        prefix = root / 'upgrade-programs'
        install('old')
        previous_identity = cell.cli(root, 'installation', 'verify')['identity']
        predecessor = root / 'predecessor-project'
        shutil.copytree(prefix / 'current/examples/projects/sales-runnable', predecessor)
        cell.cli(predecessor, 'up')
        previous_binding = cell.cli(predecessor, 'project', 'create', '--key', 'predecessor')
        assert previous_binding['definition_id'] == binding['definition_id']
        report['predecessor_database_recovery'] = seed_predecessor(cell, predecessor)
        totals(predecessor)
        install('new', upgrade=True)
        assert cell.cli(root, 'installation', 'verify')['identity'] == report['release_sha256'] != previous_identity
        cell.cli(predecessor, 'up')
        assert cell.cli(predecessor, 'project', 'installed')['preparation_needed']
        project = root / 'upgraded-project'; unpack(project)
        binding = cell.cli(project, 'project', 'attach', previous_binding['deployment_id'])
        cell.cli(project, 'branch', 'resume', 'main', '--wait')
        cell.apply(project, 'upgrade'); totals(project); notebook(project)
        check('signed_upgrade_reprepare')
        # Missing local wheel closure must fail before activation in a cold deployment.
        missing = root / 'missing'; unpack(missing)
        (missing / 'dependencies' / f'{args.target}.zip').unlink()
        cell.cli(missing, 'project', 'create', '--key', 'missing', success=False)
        check('missing_wheels_no_activation')
        # Kill at a durable adapter boundary, cancel after recovery, verify retention.
        interrupted = root / 'interrupted'; unpack(interrupted)
        cell.cli(interrupted, 'project', 'create', '--key', 'interrupted')
        cell.cli(project, 'down'); cell.env['SUPABRICKS_TEST_PROJECT_APPLY_KILL'] = 'database_owned'
        cell.cli(project, 'up')
        plan = cell.plan(interrupted)
        operation = cell.cli(interrupted, 'project', 'apply', plan, '--key', 'interrupted')
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            probe = subprocess.run([str(cell.binary), 'project', 'status', operation['id'], '--project', str(interrupted), '--data-dir', str(cell.data)], env=cell.env, capture_output=True, timeout=30)
            if probe.returncode: break
            time.sleep(.1)
        else: raise TimeoutError('apply kill checkpoint')
        cell.env.pop('SUPABRICKS_TEST_PROJECT_APPLY_KILL'); cell.cli(project, 'up')
        cell.cli(interrupted, 'project', 'cancel', operation['id'])
        cell.settle(interrupted, cell.cli(interrupted, 'project', 'status', operation['id']), 'cancelled')
        assert len(cell.cli(interrupted, 'database', 'list')['branches']) == 1
        assert cell.cli(interrupted, 'project', 'installed')['active_revision'] is None
        check('cancel_retains_resources')
        deadline = time.monotonic()+120
        while time.monotonic() < deadline:
            branch = cell.cli(interrupted, 'branch', 'get', 'main')
            if branch['revision'] == branch['observed_revision']: break
            time.sleep(.2)
        else: raise TimeoutError('retained database creation did not reconcile')
        cell.cli(interrupted, 'branch', 'resume', 'main', '--wait')
        cell.apply(interrupted, 'retry'); totals(interrupted)
        check('interrupted_apply_reconciles')
        backup = root / 'backup'; cell.cli(project, 'backup', 'create', backup)
        cell.cli(project, 'backup', 'verify', backup)
        cell.data = root / 'restored'; cell.roots.append(cell.data)
        cell.cli(project, 'backup', 'restore', backup); cell.cli(project, 'up')
        cell.cli(project, 'project', 'attach', binding['deployment_id'])
        assert cell.cli(project, 'project', 'installed')['preparation_needed']
        cell.cli(project, 'branch', 'resume', 'main', '--wait')
        cell.apply(project, 'restored'); totals(project); notebook(project)
        check('cold_restore_reprepare')
        # Dedicated bounded test volume; never fill the host filesystem.
        pressure = Path(os.environ['SUPABRICKS_PROJECT_PRESSURE_DIR'])
        before = shutil.disk_usage(pressure).free
        assert before < 160 * 1024 * 1024, 'pressure fixture must be a bounded volume'
        filler = pressure / 'fill'
        try:
            try:
                with filler.open('wb') as stream:
                    chunk = bytes(1024 * 1024)
                    for _ in range(max(0, int(before / len(chunk))-2)):
                        stream.write(chunk)
                    stream.flush(); os.fsync(stream.fileno())
            except OSError as error:
                if error.errno != errno.ENOSPC: raise
            destination = pressure / 'must-not-exist'
            rejected = cell.cli(root, 'project', 'unpack', package, '--destination', destination, success=False)
            assert 'os error 28' in json.loads(rejected.stdout or rejected.stderr)['error']['message'], 'expected real ENOSPC'
            assert not destination.exists() and not list(pressure.glob('.supabricks-package-*'))
        finally:
            filler.unlink(missing_ok=True)
        check('low_disk_no_publication')
        assert cell.cli(root, 'installation', 'verify')['identity'] == report['release_sha256']
        assert digest(package) == producer['package_sha256']
        check('final_inventory_verified')
        assert set(report['checks']) == set(CHECKS)
        report['status'] = 'passed'
    except BaseException as e:
        report.update(status='failed', failure_type=type(e).__name__)
        raise
    finally:
        for channel in channels: channel.close()
        try:
            cell.close()
        except Exception:
            report.update(status='failed', cleanup_failed=True)
        stop.set(); sampler.join(timeout=10)
        if sample_errors or sampler.is_alive(): report.update(status='failed', measurement_failed=True)
        report['measurements'].update(peak)
        if server: server.shutdown(); server.server_close()
        (root / 'preview.pem').unlink(missing_ok=True)
        save()
    assert report['status'] == 'passed', 'cleanup or measurement failed'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['bundle', 'produce', 'consume'])
    for name in ('directory', 'output', 'bundles', 'package', 'report', 'previous-directory'):
        parser.add_argument('--'+name, type=Path)
    parser.add_argument('--target', choices=TARGETS, required=True)
    parser.add_argument('--version', default='v0.1.0-alpha.36')
    parser.add_argument('--previous-version', default='v0.1.0-alpha.22')
    parser.add_argument('--release', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    required = {'bundle': ('directory', 'output'), 'produce': ('directory', 'bundles', 'output'),
                'consume': ('directory', 'previous_directory', 'package', 'report')}[args.mode]
    for name in required:
        if getattr(args, name) is None: parser.error('--'+name.replace('_', '-')+' is required')
    if args.mode == 'consume' and not os.environ.get('SUPABRICKS_PROJECT_PRESSURE_DIR'):
        parser.error('consume requires SUPABRICKS_PROJECT_PRESSURE_DIR on a dedicated bounded volume')
    for name in ('directory', 'output', 'bundles', 'package', 'report', 'previous_directory', 'release'):
        value = getattr(args, name)
        if value: setattr(args, name, value.resolve())
    if not args.release:
        root = Path(tempfile.mkdtemp(prefix='sb-pk07-release-', dir='/tmp')).resolve()
        release, _ = extract(args.directory, args.version, args.target, root)
        result = subprocess.run([str(release / 'python/runtime/bin/python3.12'), '-I', '-B', str(Path(__file__).resolve()), *sys.argv[1:], '--release', str(release)])
        if result.returncode == 0: shutil.rmtree(root)
        raise SystemExit(result.returncode)
    root = Path(tempfile.mkdtemp(prefix='sb-pk07-', dir='/tmp')).resolve(); root.chmod(0o700)
    archive = args.directory / f'supabricks-{args.version}-{args.target}.tar.gz'
    identity = dict(version=args.version, target=args.target, sha256=digest(archive))
    try:
        globals()[args.mode](args, root, args.release, identity)
    except BaseException:
        print('Private failed workspace: '+str(root), file=sys.stderr)
        raise
    shutil.rmtree(root)


if __name__ == '__main__': main()
