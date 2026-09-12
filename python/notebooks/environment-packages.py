"""NE04 registry-wheel workflows. Loaded only after component hash validation.

No project code or build backend is imported. uv edits/resolves private copies;
only the daemon may publish the declaration pair or activate a generation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tomllib
import urllib.request
from urllib.parse import urlsplit, unquote
import zipfile

MAX_DOWNLOAD = 512 * 1024**2
MAX_EXPANDED = 1024**3
INDEX = 'https://pypi.org/simple'


class Failure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    m = path.lstat()
    if not stat.S_ISREG(m.st_mode) or m.st_nlink != 1:
        raise Failure('invalid_bundle')
    return m


def wheel_entries(archive):
    entries = archive.infolist()
    if len(entries) > 100000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
        raise Failure('invalid_bundle')
    seen = set()
    for info in entries:
        name = info.filename
        parts = name.rstrip('/').split('/')
        mode = info.external_attr >> 16
        if (name in seen or not name or name.startswith('/') or '\\' in name
                or any(p in ('', '.', '..') for p in parts) or '\x00' in name
                or info.flag_bits & 1 or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
            raise Failure('invalid_bundle')
        seen.add(name)
    return entries


def checked_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != 'files.pythonhosted.org' or p.port not in (None, 443) or p.username or p.password or p.query or p.fragment:
        raise Failure('invalid_declaration')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, temporary, remaining, check):
    count = 0
    try:
        with urllib.request.build_opener(Redirect()).open(checked_url(url), timeout=15) as response, temporary.open('wb') as out:
            while chunk := response.read(1024*1024):
                count += len(chunk)
                if count > remaining:
                    raise Failure('artifact_limit')
                check()
                out.write(chunk)
    except Failure:
        raise
    except Exception as e:
        raise Failure('network_failed') from e
    return count


def execute(config, contract, package, env, check, step):
    # packaging itself is part of the verified, pure-Python kernel wheelhouse.
    # zipimport uses that immutable wheel, never the host site-packages.
    packaging = next((package / 'python/notebooks/wheelhouse').glob('packaging-*.whl'))
    sys.path.insert(0, str(packaging))
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename

    documents = Path(config['documents'])
    workflow = config['workflow']
    change = workflow['change']
    offline = workflow['offline'] or change['kind'] == 'import_bundle'
    base = contract['templates']['base']
    base_manifest = tomllib.loads((package / base['manifest']).read_text())
    protected = {p['name']: p['version'] for p in tomllib.loads((package / base['lock']).read_text())['package'] if 'registry' in p['source']}
    constraints = [f'{n}=={v}' for n, v in sorted(protected.items())]
    flags = [str(package / 'helpers/uv'), '--no-config', '--no-python-downloads']
    if offline:
        flags += ['--offline']
    project_flags = ['--project', str(documents), '--python', str(package / 'python/runtime/bin/python3.12')]
    env = dict(env, UV_HTTP_TIMEOUT='15', UV_HTTP_RETRIES='0', UV_CONCURRENT_DOWNLOADS='2', UV_CONCURRENT_INSTALLS='2')

    def uv(*args, capture=False):
        check()
        try:
            result = subprocess.run([*flags, *map(str, args)], cwd=documents, env=env, check=True,
                                    stdout=subprocess.PIPE if capture else None, timeout=55)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise Failure('resolution_failed') from e
        check()
        return result.stdout

    def requirement(text):
        try:
            r = Requirement(text)
            if r.url or len(text) > 1024:
                raise ValueError()
            if canonicalize_name(r.name) in protected and (r.extras or not r.specifier.contains(protected[canonicalize_name(r.name)], prereleases=True)):
                raise ValueError()
            return r
        except Exception as e:
            raise Failure('invalid_declaration') from e

    def declaration():
        try:
            doc = tomllib.loads((documents / 'pyproject.toml').read_text())
            project = doc['project']
            policy = doc.get('tool', {}).get('uv', {})
            if (set(doc) - {'project', 'tool'} or set(doc.get('tool', {})) - {'uv'}
                    or set(project) - {'name', 'version', 'requires-python', 'dependencies', 'description'}
                    or not SpecifierSet(project['requires-python']).contains('3.12.13', prereleases=True)
                    or len(project.get('dependencies', [])) > 128):
                raise ValueError()
            for key, value in policy.items():
                if key == 'constraint-dependencies':
                    if value != constraints:
                        raise ValueError()
                elif base_manifest['tool']['uv'].get(key) != value:
                    raise ValueError()
            dependencies = project.get('dependencies', [])
            for text in dependencies:
                requirement(text)
            return dependencies
        except Failure:
            raise
        except Exception as e:
            raise Failure('invalid_declaration') from e

    def validate_lock():
        try:
            lock = tomllib.loads((documents / 'uv.lock').read_text())
            if len(lock['package']) > 256:
                raise ValueError()
            for p in lock['package']:
                if p['source'] == {'virtual': '.'}:
                    continue
                if p['source'] != {'registry': INDEX} or (p['name'] in protected and p['version'] != protected[p['name']]):
                    raise ValueError()
                for artifact in [*p.get('wheels', []), *([p['sdist']] if p.get('sdist') else [])]:
                    checked_url(artifact['url'])
                    if not re.fullmatch('sha256:[a-f0-9]{64}', artifact['hash']):
                        raise ValueError()
                # An sdist may be recorded by uv but is never acquired or built.
            return lock
        except Failure:
            raise
        except Exception as e:
            raise Failure('invalid_declaration') from e

    old_packages = {p['name']: p['version'] for p in validate_lock()['package'] if 'registry' in p['source']}
    artifacts = Path(config['artifacts'])
    wheelhouse = documents / 'wheels'
    wheelhouse.mkdir(mode=0o700)
    download_bytes = 0

    def cache_size():
        entries = list(artifacts.iterdir())
        if len(entries) > 2048:
            raise Failure('artifact_limit')
        return sum(regular(p).st_size for p in entries)

    def cache(source, expected):
        if sha(source) != expected:
            raise Failure('invalid_bundle')
        target = artifacts / expected
        if target.exists():
            regular(target)
            if sha(target) != expected:
                raise Failure('invalid_bundle')
            return target
        if cache_size() + regular(source).st_size > config['cache_bytes']:
            raise Failure('artifact_limit')
        with source.open('rb') as src, target.open('xb') as out:
            shutil.copyfileobj(src, out, 1024*1024)
            out.flush()
            os.fsync(out.fileno())
        return target

    if change['kind'] == 'adopt':
        shutil.copyfile(documents / 'adopt.toml', documents / 'pyproject.toml')
        shutil.copyfile(documents / 'adopt.lock', documents / 'uv.lock')
    if change['kind'] == 'import_bundle':
        step('importing')
        try:
            with zipfile.ZipFile(documents / 'import.zip') as archive:
                entries = wheel_entries(archive)
                if archive.getinfo('bundle.json').file_size > 1024**2:
                    raise Failure('invalid_bundle')
                bundle = json.loads(archive.read('bundle.json'))
                if (bundle['version'] != 1 or bundle['target'] != contract['target']
                        or bundle['contract'] != config['contract'] or len(bundle['files']) > 258
                        or {i.filename for i in entries} != {'bundle.json', *bundle['files']}):
                    raise Failure('invalid_bundle')
                for name, expected in bundle['files'].items():
                    if not re.fullmatch('[a-f0-9]{64}', expected):
                        raise Failure('invalid_bundle')
                    if name not in ('pyproject.toml', 'uv.lock') and not re.fullmatch(r'wheels/[A-Za-z0-9_.+-]+\.whl', name):
                        raise Failure('invalid_bundle')
                    if name in ('pyproject.toml', 'uv.lock') and archive.getinfo(name).file_size > 1024**2:
                        raise Failure('invalid_bundle')
                    output = documents / name
                    with archive.open(name) as src, output.open('wb') as out:
                        shutil.copyfileobj(src, out, 1024*1024)
                    if sha(output) != expected:
                        raise Failure('invalid_bundle')
                    if name.startswith('wheels/'):
                        with zipfile.ZipFile(output) as wheel:
                            wheel_entries(wheel)
                        cache(output, expected)
        except Failure:
            raise
        except Exception as e:
            raise Failure('invalid_bundle') from e

    dependencies = declaration()
    validate_lock()  # Reject source overrides before allowing uv to see a lock.
    if change['kind'] in ('add', 'remove', 'lock', 'adopt'):
        step('resolving')
        if change['kind'] == 'add':
            r = requirement(change['requirement'])
            dependencies = [d for d in dependencies if canonicalize_name(requirement(d).name) != canonicalize_name(r.name)] + [str(r)]
        if change['kind'] == 'remove':
            name = canonicalize_name(change['package'])
            if not re.fullmatch('[a-z0-9][a-z0-9-]*', name) or name in protected:
                raise Failure('invalid_declaration')
            if not any(canonicalize_name(requirement(d).name) == name for d in dependencies):
                raise Failure('invalid_declaration')
            dependencies = [d for d in dependencies if canonicalize_name(requirement(d).name) != name]
        names = {canonicalize_name(requirement(d).name) for d in dependencies}
        for root in base_manifest['project']['dependencies']:
            if canonicalize_name(requirement(root).name) not in names:
                dependencies.append(root)
        text = '[project]\nname = "supabricks-notebook-kernel"\nversion = "0.1.0"\nrequires-python = "==3.12.13"\ndependencies = ' + json.dumps(dependencies) + '\n\n[tool.uv]\n'
        for key, value in base_manifest['tool']['uv'].items():
            text += key + ' = ' + json.dumps(value) + '\n'
        text += 'constraint-dependencies = ' + json.dumps(constraints) + '\n'
        (documents / 'pyproject.toml').write_text(text)
        # PySpark Client is the single builder-qualified sdist exception in
        # the release. Resolve using its shipped wheel, without invoking a build
        # backend. Restore its exact upstream lock block so no local path leaks
        # into the portable lock. All other packages still require registry wheels.
        qualified = documents / 'qualified'
        qualified.mkdir()
        spark = next((package / 'python/notebooks/wheelhouse').glob('pyspark_client-*.whl'))
        shutil.copyfile(spark, qualified / spark.name)
        uv('lock', *project_flags, '--default-index', INDEX, '--no-build', '--find-links', qualified)
        pattern = r'(?=^\[\[package\]\]$)'
        original = (package / base['lock']).read_text()
        spark_block = next(b for b in re.split(pattern, original, flags=re.M)[1:] if tomllib.loads(b)['package'][0]['name'] == 'pyspark-client')
        blocks = re.split(pattern, (documents / 'uv.lock').read_text(), flags=re.M)
        (documents / 'uv.lock').write_text(''.join(spark_block if b.startswith('[[package]]') and tomllib.loads(b)['package'][0]['name'] == 'pyspark-client' else b for b in blocks))
    lock = validate_lock()
    # --locked verifies declared intent, without silently updating the lock.
    exported = uv('export', *project_flags, '--locked', '--no-build', '--no-emit-project', '--no-dev', '--format', 'requirements-txt', capture=True).decode()
    selected = {}
    for line in exported.replace('\\\n', ' ').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        req = Requirement(line.split('--hash=')[0].strip())
        if req.url or (req.marker and not req.marker.evaluate()):
            if req.url:
                raise Failure('invalid_declaration')
            continue
        specs = list(req.specifier)
        if len(specs) != 1 or specs[0].operator != '==':
            raise Failure('invalid_declaration')
        selected[canonicalize_name(req.name)] = specs[0].version
    if any(selected.get(n) != v for n, v in base['packages'].items()):
        raise Failure('invalid_declaration')
    tags = set(sys_tags())
    bundled = {}
    for path in (package / 'python/notebooks/wheelhouse').iterdir():
        name, version, _, wheel_tags = parse_wheel_filename(path.name)
        if wheel_tags & tags:
            bundled[(name, str(version))] = path
    requirements, used = [], {}
    expanded = 0
    step('acquiring')
    for name, version in sorted(selected.items()):
        check()
        source = bundled.get((name, version))
        if source is not None:
            expected = sha(source)
            records = [p for p in lock['package'] if p['name'] == name and p['version'] == version]
            if name == 'pyspark-client':
                qualified = next(p for p in tomllib.loads((package / base['lock']).read_text())['package'] if p['name'] == name)
                if len(records) != 1 or records[0].get('sdist') != qualified.get('sdist'):
                    raise Failure('artifact_hash_mismatch')
            elif not any(a['hash'] == 'sha256:' + expected for p in records for a in p.get('wheels', [])):
                raise Failure('artifact_hash_mismatch')
            filename = source.name
        else:
            candidates = [a for p in lock['package'] if p['name'] == name and p['version'] == version for a in p.get('wheels', [])]
            artifact = next((a for a in candidates if parse_wheel_filename(unquote(urlsplit(a['url']).path.rsplit('/',1)[-1]))[3] & tags), None)
            if artifact is None:
                raise Failure('invalid_declaration')
            expected = artifact['hash'].removeprefix('sha256:')
            filename = unquote(urlsplit(artifact['url']).path.rsplit('/',1)[-1])
            if not re.fullmatch(r'[A-Za-z0-9_.+-]+\.whl', filename):
                raise Failure('invalid_declaration')
            source = artifacts / expected
            if source.exists():
                regular(source)
                if sha(source) != expected:
                    raise Failure('invalid_bundle')
            elif offline:
                raise Failure('offline_artifacts_missing')
            else:
                temporary = documents / 'download'
                download_bytes += download(artifact['url'], temporary, MAX_DOWNLOAD-download_bytes, check)
                source = cache(temporary, expected)
                temporary.unlink()
        with zipfile.ZipFile(source) as archive:
            expanded += sum(i.file_size for i in wheel_entries(archive))
            if expanded > MAX_EXPANDED:
                raise Failure('artifact_limit')
        destination = wheelhouse / filename
        if destination.exists():
            regular(destination)
            if sha(destination) != expected:
                raise Failure('invalid_bundle')
        else:
            shutil.copyfile(source, destination)
        used['wheels/' + filename] = expected
        requirements.append(f'{name}=={version} --hash=sha256:{expected}\n')
    (documents / 'requirements.lock').write_text(''.join(requirements))
    bundle_path = None
    if change['kind'] == 'export_bundle':
        step('exporting')
        target = Path(change['path'])
        if not target.is_absolute() or target.parent.resolve() != target.parent:
            raise Failure('invalid_bundle')
        files = {**used, 'pyproject.toml':sha(documents / 'pyproject.toml'), 'uv.lock':sha(documents / 'uv.lock')}
        # Build privately, publish to the explicit destination only after the
        # environment passes verification (the caller invokes this closure).
        archive_path = documents / 'export.zip'
        with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('bundle.json', json.dumps(dict(version=1,target=contract['target'],contract=config['contract'],files=files)))
            for name in sorted(files):
                archive.write(documents / name, name)
        if archive_path.stat().st_size > MAX_DOWNLOAD:
            raise Failure('artifact_limit')
        bundle_path = str(target)
    locked_packages = {p['name']: p['version'] for p in lock['package'] if 'registry' in p['source']}
    changes = [{'package':n,'before':old_packages.get(n),'after':locked_packages.get(n)} for n in sorted(old_packages.keys() | locked_packages.keys()) if old_packages.get(n) != locked_packages.get(n)]
    return dict(requirements=str(documents / 'requirements.lock'), packages=selected, wheelhouse=str(wheelhouse),
                changes=changes, network='offline' if offline else INDEX, bundle=bundle_path)
