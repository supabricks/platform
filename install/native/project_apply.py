"""PK04 exact-runtime qualification, shared by archive and local source checks."""
import json
from pathlib import Path
import shutil
import subprocess
import time


def qualify(binary, release, source_project, workspace, env):
    def deployment_cli(path, *args):
        result = subprocess.run([str(binary), *map(str, args), '--project', str(path)],
                                env=env, capture_output=True, text=True, timeout=180)
        if result.returncode:
            (workspace / 'pk04-command.log').write_text(result.stdout + result.stderr)
            raise RuntimeError('PK04 CLI failed; inspect private pk04-command.log')
        return json.loads(result.stdout)
    # PK04 applies a reviewed source plan, with real offline environment preparation.
    applied = workspace / 'applied-project'
    shutil.copytree(source_project, applied)
    template = release / 'python/notebooks/environments/base'
    for name in ('pyproject.toml', 'uv.lock'):
        shutil.copy2(template / name, applied / 'notebooks/environment' / name)
    deployment_cli(applied, 'project', 'create', '--key', 'pk04-deployment')
    plan = deployment_cli(applied, 'project', 'plan')
    assert plan == deployment_cli(applied, 'project', 'plan')
    assert deployment_cli(applied, 'database', 'list')['branches'] == []
    plan_file = workspace / 'reviewed-plan.json'
    plan_file.write_text(json.dumps(plan))
    operation = deployment_cli(applied, 'project', 'apply', plan_file, '--key', 'apply-first')
    deadline = time.monotonic() + 180
    while operation['state'] in ('queued', 'preparing', 'activating'):
        assert time.monotonic() < deadline, 'PK04 apply timeout'
        time.sleep(0.2)
        operation = deployment_cli(applied, 'project', 'status', operation['id'])
    assert operation['state'] == 'succeeded', operation.get('error')
    assert deployment_cli(applied, 'project', 'apply', plan_file, '--key', 'apply-first')['id'] == operation['id']
    installed = deployment_cli(applied, 'project', 'installed')
    assert installed['active_revision'] == operation['id'] and not installed['preparation_needed']
    environment = Path(installed['environment_worktrees']['environment.notebook'])
    environment_status = deployment_cli(environment, 'env', 'status')
    assert not environment_status['preparation_needed']
    asset = deployment_cli(applied, 'project', 'asset', 'notebook.sales')
    assert asset['read_only'] and (environment / 'notebooks/sales.ipynb').exists()
    original = asset['content']
    draft = json.loads((environment / 'notebooks/sales.ipynb').read_text())
    draft['metadata']['pk04_draft'] = True
    (environment / 'notebooks/sales.ipynb').write_text(json.dumps(draft))
    assert deployment_cli(applied, 'project', 'asset', 'notebook.sales')['content'] == original
    assert deployment_cli(applied, 'sql', '--branch', 'main', '--sql', "SELECT to_regclass('public.sales')")['rows'] == [[None]]
    deployment_cli(applied, 'branch', 'suspend', 'main', '--wait')
    return 'PK04 installed plan/apply/replay, real PostgreSQL allocation and offline managed environment, atomic active revision and immutable notebook with editable draft; declarations never execute SQL or fixtures'
