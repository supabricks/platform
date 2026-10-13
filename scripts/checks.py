#!/usr/bin/env python3
"""Shared change selection for local feedback and GitHub checks (standard library only)."""
import argparse
import ast
import fnmatch
import json
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]
AREAS = {'core', 'local', 'operator', 'cluster', 'ui', 'console',
         'notebooks', 'components', 'analytics', 'policy'}
# Multiple rules may match. Unknown executable/configuration paths select all.
RULES = {
    'crates/core/**': {'core', 'local', 'operator', 'cluster'},
    'crates/local/**': {'local'},
    'crates/operator/**': {'operator', 'cluster'},
    'chart/**': {'operator', 'cluster'},
    'ui/**': {'ui', 'operator', 'cluster'},
    'console': {'console'},
    'console/**': {'console'},
    '.gitmodules': {'console'},
    'components/**': {'components'},
    'components/components.lock.json': {'analytics'},
    'components/*deltalake*': {'analytics'},
    'components/*sail*': {'analytics'},
    'components/patches/**': {'analytics'},
    'components/requirements.txt': {'components', 'analytics'},
    'python/analytics/**': {'analytics', 'components'},
    'python/ingest/**': {'analytics'},
    'python/notebooks/**': {'notebooks', 'components'},
    'python/execution/**': {'notebooks'},
    'schemas/**': {'core', 'local', 'operator', 'components'},
    'examples/projects/**': {'local', 'components'},
    'examples/orders/**': {'local', 'cluster'},
    'examples/console/**': {'console'},
    'examples/notebooks/**': {'notebooks'},
    'agents/**': {'local'},
    'install/native/**': {'components'},
    'install/native/delta_runtime.py': {'analytics'},
    'install/native/analytics.py': {'analytics'},
    'install/native/*upgrade*': {'local'},
    'install/*.sh': {'cluster'},
    'install/*.yaml': {'cluster'},
    'install/minio/**': {'cluster'},
    'e2e/*.sh': {'cluster'},
    'e2e/*.py': {'cluster'},
    'e2e/native/**': set(),  # Dedicated native workflows own these runtime fixtures.
    'e2e/native/console_health.py': {'local'},
    'e2e/tpcds/**': set(),  # tpcds-inputs owns its contracts and source inventory.
    'scripts/check-portable-deps.py': {'core', 'local'},
    '.github/actions/deltalake/**': {'components', 'analytics'},
    '.github/actions/portable-check/**': {'core', 'local'},
}
ALL_INPUTS = ('Cargo.toml', 'Cargo.lock', 'rust-toolchain*', '.cargo/**',
              'Dockerfile*', '.dockerignore', 'Justfile', 'scripts/checks.py',
              'scripts/test_checks.py', '.github/workflows/ci.yml',
              '.github/workflows/native-baseline.yml', '.github/workflows/check-scope.yml')


def matches(name, pattern):
    # fnmatch alone lets '*' cross '/', unlike workflow path globs.
    path, parts = name.split('/'), pattern.split('/')
    def visit(path, parts):
        if not parts:
            return not path
        if parts[0] == '**':
            return any(visit(path[i:], parts[1:]) for i in range(len(path)+1))
        return bool(path) and fnmatch.fnmatchcase(path[0], parts[0]) and visit(path[1:], parts[1:])
    return visit(path, parts)


def select(paths):
    areas = set()
    for name in paths:
        if any(matches(name, pattern) for pattern in ALL_INPUTS):
            areas.update(AREAS)
            continue
        matched = [value for pattern, value in RULES.items() if matches(name, pattern)]
        if matched:
            areas.update(set().union(*matched))
        elif name.startswith('docs/'):
            # Evidence is read by the inventory validator. Prose needs no builds.
            if not name.endswith(('.md', '.png', '.jpg', '.svg')):
                areas.add('components')
        elif name.endswith('.md') or name in ('LICENSE', '.gitignore'):
            pass
        elif name.startswith('.github/'):
            areas.add('policy')
        else:
            areas.update(AREAS)
    result = {area: area in areas for area in sorted(AREAS)}
    result['fmt'] = bool(areas & {'core', 'local', 'operator'})
    result['portable'] = bool(areas & {'core', 'local'})
    result['unit'] = bool(areas & {'core', 'local', 'operator', 'ui', 'console', 'notebooks', 'policy'})
    return result


def git(*args, root=ROOT):
    return subprocess.check_output(['git', *args], cwd=root)


def changed(base, *, root=ROOT, working=True):
    # Disable rename collapsing: both the removed and added paths affect scope.
    end = [] if working else ['HEAD']
    names = git('diff', '--name-only', '--no-renames', '-z', base, *end, '--', root=root)
    if working:
        names += git('ls-files', '--others', '--exclude-standard', '-z', root=root)
    return sorted(set(os.fsdecode(p) for p in names.split(b'\0') if p))


def github_scope():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    base = event.get('pull_request', {}).get('base', {}).get('sha') or event.get('before')
    if not base or set(base) == {'0'}:
        return [], select(['Cargo.lock'])
    try:
        # Compare the tested merge tree to its base, with no API file-list limit.
        subprocess.run(['git', 'fetch', '--no-tags', '--depth=1', 'origin', base], cwd=ROOT, check=True)
        paths = changed(base, working=False)
        return paths, select(paths)
    except subprocess.CalledProcessError:
        print('Cannot establish change scope; selecting every check.', flush=True)
        return [], select(['Cargo.lock'])


def local_commands(scope, paths=()):
    commands = []
    if scope['fmt']:
        commands.append(['cargo', 'fmt', '--all', '--', '--check'])
    packages = [package for area, package in (('core', 'supabricks-core'),
                ('local', 'supabricks-local')) if scope[area]]
    if packages:
        commands.append(['cargo', 'test', '--locked', '--lib', *sum((['-p', p] for p in packages), [])])
    if scope['operator']:
        commands.append(['cargo', 'test', '--locked', '-p', 'sspc-operator', '--bins'])
    # If a concrete integration test is edited, run it without sweeping every
    # recovery fixture. Shared source changes still leave the full sweep to CI.
    for crate, package in (('core', 'supabricks-core'), ('local', 'supabricks-local'),
                           ('operator', 'sspc-operator')):
        tests = sorted({Path(name).stem for name in paths
                        if Path(name).parent.as_posix() == f'crates/{crate}/tests'
                        and name.endswith('.rs') and (ROOT/name).is_file()})
        if tests:
            commands.append(['cargo', 'test', '--locked', '-p', package,
                             *sum((['--test', test] for test in tests), [])])
    if scope['portable']:
        commands.append(['python3', 'scripts/check-portable-deps.py'])
    if scope['components']:
        commands.extend([['python3', 'components/validate.py'],
                         ['python3', '-m', 'unittest', 'discover', '-s', 'components', '-v']])
    if scope['notebooks']:
        commands.append(['python3', '-m', 'unittest', 'discover', '-s', 'python/notebooks',
                         '-p', 'test_environment_packages.py'])
    if scope['policy']:
        commands.append(['python3', '-m', 'unittest', 'discover', '-s', 'scripts', '-v'])
    for directory in ('ui', 'console'):
        if scope[directory]:
            commands.append(['npm', 'ci', '--prefix', directory, '--no-audit', '--no-fund'])
            if directory == 'ui':
                commands.append(['npm', 'test', '--prefix', directory])
            commands.append(['npm', 'run', 'build', '--prefix', directory])
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='HEAD', help='compare this tree to working files (includes untracked)')
    parser.add_argument('--plan', action='store_true', help='show scope/commands without running checks')
    parser.add_argument('--github', action='store_true', help='emit CI outputs; never execute local checks')
    args = parser.parse_args()
    paths, scope = github_scope() if args.github else (None, None)
    if paths is None:
        paths = changed(args.base)
        scope = select(paths)
    print(json.dumps(dict(paths=paths, checks=scope), indent=2), flush=True)
    if args.github:
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for name, enabled in scope.items():
                output.write(f'{name}={str(enabled).lower()}\n')
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                selected = ', '.join(name for name in sorted(AREAS) if scope[name])
                summary.write('Selected checks: '+(selected or 'none (unrelated changes)')+'\n')
        return
    commands = local_commands(scope, paths)
    for command in commands:
        print(shlex.join(command), flush=True)
    print('Local scope: library/unit checks and builds above; integration, native and analytical qualification run in GitHub.', flush=True)
    if args.plan:
        return
    for name in paths:
        path = ROOT / name
        if path.suffix == '.py' and path.is_file():
            ast.parse(path.read_bytes(), filename=name)
    for command in commands:
        subprocess.run(command, cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
