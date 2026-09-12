"""Owned environment worker. Only explicit package operations may use the network."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('x') as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def regular(path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError('component or runtime file was substituted')


def tree(path, max_bytes, *, sync=False):
    """Inventory regular bytes and link text; never dereference a tree symlink."""
    hashes, allocated, durable = {}, 0, []
    for directory, directories, files in os.walk(path, followlinks=False):
        for name in list(directories) + files:
            entry = Path(directory) / name
            m = entry.lstat()
            relative = str(entry.relative_to(path))
            if stat.S_ISLNK(m.st_mode):
                hashes[relative] = {'link': os.readlink(entry)}
            elif stat.S_ISREG(m.st_mode):
                if m.st_nlink != 1:
                    raise ValueError('environment must not contain mutable hard links')
                allocated += m.st_blocks * 512
                if allocated > max_bytes or len(hashes) >= 100000:
                    raise ValueError('environment size limit')
                hashes[relative] = digest(entry)
                if sync:
                    durable.append(entry)
            elif not stat.S_ISDIR(m.st_mode):
                raise ValueError('unexpected environment file type')
        if sync:
            durable.append(Path(directory))
    def flush(path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if sync:
        # Serial per-file fsync creates thousands of separate journal commits.
        # A bounded batch permits filesystem group commit while still checking
        # every flush before the daemon can publish a generation as ready.
        with ThreadPoolExecutor(max_workers=16) as pool:
            for _ in pool.map(flush, durable):
                pass
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(), allocated


def main():
    config_path = Path(sys.argv[1])
    regular(config_path)
    config = json.loads(config_path.read_text())
    assert config['version'] == 1
    package = Path(config['package'])
    contract_path = package / 'python/notebooks/kernel-contract.json'
    regular(contract_path)
    assert digest(contract_path) == config['contract']
    contract = json.loads(contract_path.read_text())
    for name, expected in contract['files'].items():
        path = Path(name)
        assert not path.is_absolute() and all(p not in ('.', '..') for p in path.parts)
        current = package
        for component in path.parts:
            current = current / component
            assert not current.is_symlink()
        regular(current)
        assert digest(current) == expected, 'qualified component changed'
    generation = Path(config['generation'])
    descriptor = os.open(generation, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    def check():
        m = generation.lstat()
        assert stat.S_ISDIR(m.st_mode) and [m.st_dev, m.st_ino] == config['directory']
        held = os.fstat(descriptor)
        assert (held.st_dev, held.st_ino) == (m.st_dev, m.st_ino)
        assert shutil.disk_usage(generation).free >= config['reserve_bytes']
        for name in ['cache', 'home']:
            m = Path(config[name]).lstat()
            assert stat.S_ISDIR(m.st_mode) and m.st_uid == os.getuid() and not m.st_mode & 0o077
    def step(stage):
        check()
        atomic(Path(config['progress']), {'stage': stage})
    env = {'HOME': config['home'], 'PATH': '', 'LANG': 'C.UTF-8',
           'UV_CACHE_DIR': config['cache'], 'UV_PYTHON_DOWNLOADS': 'never',
           'PYTHONDONTWRITEBYTECODE': '1',
           'SUPABRICKS_PROCESS_TOKEN': os.environ['SUPABRICKS_PROCESS_TOKEN']}
    uv = str(package / 'helpers/uv')
    python = package / 'python/runtime/bin/python3.12'
    template = contract['templates'][config['template']]
    workflow_result = {}
    wheelhouse = package / 'python/notebooks/wheelhouse'
    def run(command):
        check()
        # fchdir anchors the child to the opened final directory. uv creates
        # at '.', so venv prefixes contain its canonical final path, not an fd.
        subprocess.run(list(map(str, command)), env=env, check=True,
                       preexec_fn=lambda: os.fchdir(descriptor), timeout=55)
        check()
    flags = [uv, '--no-config', '--offline', '--no-python-downloads']
    try:
        if config.get('workflow'):
            import importlib.util
            spec = importlib.util.spec_from_file_location('environment_packages', package / 'python/notebooks/environment-packages.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            try:
                workflow_result = module.execute(config, contract, package, env, check, step)
            except module.Failure as error:
                atomic(Path(config['report']).with_name('error.json'), {'code': error.code})
                raise
            template = workflow_result
            wheelhouse = Path(template['wheelhouse'])
        step('creating')
        run([*flags, 'venv', '--python', python, '.'])
        assert 'include-system-site-packages = false' in (generation / 'pyvenv.cfg').read_text()
        step('syncing')
        run([*flags, 'pip', 'sync', '--python', './bin/python', '--no-index',
             '--find-links', wheelhouse, '--only-binary', ':all:',
             '--require-hashes', '--link-mode', 'copy', package / template['requirements']])
        step('verifying')
        run([*flags, 'pip', 'check', '--python', './bin/python'])
        code = """import json,sys,site,importlib.metadata as md
import ipykernel,pyarrow,pandas,pyspark.sql
print(json.dumps(dict(prefix=sys.prefix,base=sys.base_prefix,user=site.ENABLE_USER_SITE,
packages={d.metadata['Name'].lower().replace('_','-'):d.version for d in md.distributions()})))
"""
        result = json.loads(subprocess.check_output(['./bin/python', '-I', '-B', '-c', code], env=env,
                            preexec_fn=lambda: os.fchdir(descriptor), timeout=15))
        assert result['prefix'] == str(generation) and result['base'] == str(python.parent.parent)
        assert not result['user'] and result['packages'] == template['packages']
        # Durably sync package bytes before SQLite can publish ready metadata.
        fingerprint, allocated = tree(generation, config['max_bytes'], sync=True)
        check()
        # Cache remains disposable. Collection is serialized with preparation;
        # never touch installed generations to meet a cache budget.
        _, cache_size = tree(Path(config['cache']), 8 * 1024**3)
        if cache_size > config['cache_bytes']:
            run([*flags, 'cache', 'clean'])
        if workflow_result.get('bundle'):
            target = Path(workflow_result['bundle'])
            if target.parent.resolve() != target.parent:
                raise ValueError('bundle parent changed')
            parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                name = '.supabricks-bundle-' + config['operation_id'] + '.tmp'
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with (Path(config['documents']) / 'export.zip').open('rb') as src, os.fdopen(fd, 'wb') as out:
                    shutil.copyfileobj(src, out, 1024*1024)
                    out.flush()
                    os.fsync(out.fileno())
                os.fsync(parent)
            finally:
                os.close(parent)
        if config.get('workflow'):
            tree(Path(config['documents']), 3 * 1024**3, sync=True)
        atomic(Path(config['report']), {**{k: workflow_result.get(k) for k in ('changes', 'network', 'bundle')}, 'status': 'ready' , 'prefix': result['prefix'],
               'contract': config['contract'], 'packages': result['packages'],
               'inventory': fingerprint, 'allocated_bytes': allocated})
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
