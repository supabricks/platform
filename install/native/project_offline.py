"""PK05 exact-runtime offline project qualification; no package resolution or hooks."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


def qualify(binary, release, source_project, workspace, env):
    # macOS /tmp aliases /private/tmp; NE04 publication requires canonical parents.
    workspace = workspace.resolve()
    def cli(path, *args, fail=False):
        project_args = [] if args[:2] == ('project', 'unpack') else ['--project', str(path)]
        result = subprocess.run([str(binary), *map(str, args), *project_args],
                                env=env, capture_output=True, text=True, timeout=240)
        if fail:
            assert result.returncode != 0
            return
        if result.returncode:
            (workspace / 'pk05-command.log').write_text(result.stdout + result.stderr)
            diagnostic = {}
            try:
                value = json.loads(result.stdout)
                diagnostic = {key:value.get(key) for key in ('state', 'error')}
            except ValueError:
                pass
            # Environment errors are bounded typed reasons, with no source contents.
            detail = str(diagnostic) if args[:1] == ('env',) else ''
            raise RuntimeError('PK05 CLI '+str(args[:2])+' failed; '+detail+'; inspect private pk05-command.log')
        return json.loads(result.stdout)

    def apply(path, key, state='succeeded'):
        plan = cli(path, 'project', 'plan')
        plan_file = workspace / 'pk05-plan.json'
        plan_file.write_text(json.dumps(plan))
        operation = cli(path, 'project', 'apply', plan_file, '--key', key)
        deadline = time.monotonic() + 240
        while operation['state'] in ('queued', 'preparing', 'activating'):
            assert time.monotonic() < deadline, 'PK05 apply timeout'
            time.sleep(.3)
            operation = cli(path, 'project', 'status', operation['id'])
        assert operation['state'] == state, operation.get('error')
        assert cli(path, 'project', 'apply', plan_file, '--key', key)['id'] == operation['id']
        return operation

    # Use NE04's actual export, including its verification and no-replace publication.
    exporter = workspace / 'pk05-exporter'
    exporter.mkdir()
    cli(exporter, 'init', 'bundle-exporter')
    cli(exporter, 'env', 'init', '--wait')
    bundle = workspace / 'kernel-bundle.zip'
    cli(exporter, 'env', 'export-bundle', bundle, '--offline', '--wait')
    source = workspace / 'pk05-source'
    shutil.copytree(source_project, source)
    template = exporter / 'notebooks/environment'
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copy2(template / name, source / 'notebooks/environment' / name)
    contract = json.loads((release / 'python/notebooks/kernel-contract.json').read_text())
    (source / 'dependencies').mkdir()
    shutil.copy2(bundle, source / 'dependencies/kernel.zip')
    manifest = source / 'supabricks.toml'
    text = manifest.read_text().replace('"migrations/*.sql"]', '"migrations/*.sql", "dependencies/*.zip"]')
    text += '\n[environments.notebook.bundles]\n' + contract['target'] + ' = "dependencies/kernel.zip"\n'
    manifest.write_text(text)
    package = workspace / 'sales-offline.sbproj'
    report = cli(source, 'project', 'pack', '--output', package)
    closure = report['inspection']['environments']['notebook']['bundles'][contract['target']]
    assert closure['wheels'] > 0 and closure['status'] == 'inventory_verified_runtime_preparation_required'
    destination = workspace / 'pk05-installed'
    cli(source, 'project', 'unpack', package, '--destination', destination)
    cli(destination, 'project', 'create', '--key', 'pk05-create')
    first = apply(destination, 'pk05-first')
    installed = cli(destination, 'project', 'installed')
    assert installed['active_revision'] == first['id'] and not installed['preparation_needed']
    assert first['resources']['fixture.sales']['receipt']['committed_rows'] == 2
    for name in ('create_marker', 'seed_marker'):
        assert first['resources']['migration.' + name]['receipt']['boundary'] == 'committed'
    def sql(query):
        return cli(destination, 'sql', '--branch', 'main', '--sql', query)['rows']
    assert sql('SELECT count(*),sum(amount) FROM public.sales') == [['2', '30']]
    assert sql('SELECT count(*) FROM public.project_marker') == [['1']]
    second = apply(destination, 'pk05-reconcile')
    assert second['resources']['fixture.sales']['receipt']['job'] == first['resources']['fixture.sales']['receipt']['job']
    assert sql('SELECT count(*) FROM public.project_marker') == [['1']]
    assert sql('SELECT count(*) FROM public.sales') == [['2']]

    # A failed statement cannot change the active pointer or undo earlier commits.
    migration = destination / 'migrations/003-failure.sql'
    migration.write_text('CREATE TABLE public.should_rollback AS SELECT 1/0 AS value;\n')
    manifest = destination / 'supabricks.toml'
    manifest.write_text(manifest.read_text() + '\n[resources.migration.failure]\nkind="migration"\nfile="migrations/003-failure.sql"\ndatabase="database.main"\nsequence=3\n')
    failed = apply(destination, 'pk05-failure', 'failed')
    assert '22012' in failed['error']
    assert cli(destination, 'project', 'installed')['active_revision'] == second['id']
    assert sql("SELECT to_regclass('public.should_rollback')") == [[None]]
    assert sql('SELECT count(*) FROM public.project_marker') == [['1']]
    migration.write_text('CREATE TABLE public.recovered (id integer);\n')
    recovered = apply(destination, 'pk05-recovered')
    # A committed identity cannot be edited into a second execution.
    migration.write_text('CREATE TABLE public.changed (id integer);\n')
    changed = apply(destination, 'pk05-checksum', 'failed')
    assert 'checksum' in changed['error']
    assert sql("SELECT to_regclass('public.changed')") == [[None]]
    assert cli(destination, 'project', 'installed')['active_revision'] == recovered['id']
    migration.write_text('CREATE TABLE public.recovered (id integer);\n')

    # Extended Parse must reject a script attempting an explicit COMMIT before any DML.
    (destination / 'migrations/004-multiple.sql').write_text('INSERT INTO public.project_marker VALUES (2); COMMIT;')
    manifest.write_text(manifest.read_text() + '\n[resources.migration.multiple]\nkind="migration"\nfile="migrations/004-multiple.sql"\ndatabase="database.main"\nsequence=4\n')
    multiple = apply(destination, 'pk05-multiple', 'failed')
    assert '42601' in multiple['error']
    assert sql('SELECT count(*) FROM public.project_marker') == [['1']]

    session = cli(destination, 'analytics', 'open', '--branch', 'main', '--ttl-ms', '600000', '--wait')
    result = cli(destination, 'analytics', 'sql', '--session', session['id'], '--sql', 'SELECT sum(amount) FROM public.sales')
    assert result['rows'] == [['30']]
    cli(destination, 'analytics', 'close', session['id'], '--wait')
    installed = cli(destination, 'project', 'installed')
    environment = Path(installed['environment_worktrees']['environment.notebook'])
    generation = installed['resources']['environment.notebook']['generation']
    # Execute the packaged notebook explicitly through the real browser transport
    # in the release Python, whose websocket/Jupyter dependencies are qualified.
    script = workspace / 'pk05-notebook.py'
    client_root = Path(__file__).resolve().parents[2] / 'e2e/native/notebook-environments'
    script.write_text('''import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from client import Console, execute
binary, project, generation = sys.argv[2:]
def cli(path,*args):
    p=subprocess.run([binary,*args,'--project',str(path)],capture_output=True,text=True,timeout=180)
    if p.returncode: raise RuntimeError('notebook CLI failed')
    return json.loads(p.stdout)
channels=[]
c=Console(Path(project),cli,channels)
e=c.start(generation)
try:
    ws=c.connect(e)
    doc=json.loads((Path(project)/'notebooks/sales.ipynb').read_text())
    for cell in doc['cells']:
        if cell['cell_type']=='code': execute(ws,''.join(cell['source']))
    execute(ws,"assert spark.table('public.sales').count()==2\\nassert spark.sql('SELECT sum(amount) FROM public.sales').first()[0]==30\\nassert supabricks_environment['id']=="+repr(generation))
    assert e['epoch_id'] and e['environment']['contract'] and e['environment']['inputs']
finally:
    for ws in channels: ws.close()
    c.stop(e)
''')
    completed = subprocess.run([str(release / 'python/runtime/bin/python3.12'), '-I', '-B', str(script),
                               str(client_root), str(binary), str(environment), generation],
                              env=env, capture_output=True, text=True, timeout=240)
    (workspace / 'pk05-notebook.log').write_text(completed.stdout + completed.stderr)
    if completed.returncode:
        raise RuntimeError('PK05 notebook failed; inspect private pk05-notebook.log')
    cli(destination, 'branch', 'suspend', 'main', '--wait')
    qualify_crash(binary, workspace, env)
    return 'PK05 NE04 wheel export, portable package transfer, offline import and managed kernel; transactional ordered migrations including SIGKILL after COMMIT, fresh-table fixture receipts/retry/checksum failure; PostgreSQL/Spark totals and explicitly executed notebook with epoch/environment provenance'


def qualify_crash(binary, workspace, env):
    """Kill after PG COMMIT, before the apply journal observes it, then replay."""
    import uuid
    project = workspace / 'pk05-crash'
    project.mkdir()
    (project / 'migrations').mkdir()
    (project / 'migrations/first.sql').write_text('CREATE TABLE public.exactly_once (id integer);\n')
    (project / 'migrations/second.sql').write_text('INSERT INTO public.exactly_once VALUES (1);\n')
    (project / 'supabricks.toml').write_text(f'''format_version=2
id="{uuid.uuid4()}"
name="migration-crash"
[package]
version="0.1.0"
include=["migrations/*.sql"]
notebook_outputs="strip"
[resources.database.main]
kind="postgres_database"
lifecycle="retain"
[resources.migration.first]
kind="migration"
file="migrations/first.sql"
database="database.main"
sequence=1
[resources.migration.second]
kind="migration"
file="migrations/second.sql"
database="database.main"
sequence=2
''')
    local_env = dict(env)
    def cli(*args, expect=True):
        r = subprocess.run([str(binary), *map(str, args), '--project', str(project)], env=local_env,
                           capture_output=True, text=True, timeout=180)
        if not expect:
            return r
        if r.returncode:
            (workspace / 'pk05-crash.log').write_text(r.stdout + r.stderr)
            raise RuntimeError('PK05 crash fixture CLI failed')
        return json.loads(r.stdout)
    cli('project', 'create', '--key', 'crash')
    cli('down')
    local_env['SUPABRICKS_TEST_PROJECT_APPLY_KILL'] = 'migration_committed'
    cli('up')
    plan = workspace / 'pk05-crash-plan.json'
    plan.write_text(json.dumps(cli('project', 'plan')))
    operation = cli('project', 'apply', plan, '--key', 'crash-apply')
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        r = cli('project', 'status', operation['id'], expect=False)
        if r.returncode:
            break
        state = json.loads(r.stdout)['state']
        assert state not in ('succeeded', 'failed', 'cancelled'), 'expected COMMIT checkpoint kill'
        time.sleep(.2)
    else:
        raise TimeoutError('migration COMMIT kill did not occur')
    local_env.pop('SUPABRICKS_TEST_PROJECT_APPLY_KILL')
    cli('up')
    while time.monotonic() < deadline:
        status = cli('project', 'status', operation['id'])
        if status['state'] not in ('queued', 'preparing', 'activating'):
            break
        time.sleep(.2)
    assert status['state'] == 'succeeded', status.get('error')
    assert cli('sql', '--branch', 'main', '--sql', 'SELECT count(*) FROM public.exactly_once')['rows'] == [['1']]
    assert cli('project', 'apply', plan, '--key', 'crash-apply')['id'] == operation['id']
    cli('branch', 'suspend', 'main', '--wait')
