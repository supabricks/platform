"""Bind a retained-data analytical qualification to a reviewed Sail artifact.

This deliberately admits only a same-version Sail wheel replacement plus the
native recovery binary admitted by verify.py. Every other payload remains exact.
It is an engineering qualification, not a signed distribution or upgrade promise.
"""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'install/native'))
from sail import verify as verify_sail


def digest(data):
    return hashlib.sha256(data).hexdigest()


def replacement_files(artifact, target):
    """Expected bytes, derived exclusively from the verified source-built wheel."""
    wheel, report = verify_sail(artifact, target)
    with zipfile.ZipFile(wheel) as archive:
        files = {}
        for name in archive.namelist():
            if name.endswith('/'):
                continue
            path = Path(name)
            assert not path.is_absolute() and '..' not in path.parts
            assert name.startswith(('pysail/', 'pysail-' + report['version'] + '.dist-info/'))
            files['python/runtime/lib/python3.12/site-packages/' + name] = archive.read(name)
    for name in ('Cargo.lock', 'LICENSE', 'dependencies.json', 'sail-build.json'):
        files['provenance/sail/' + name] = (artifact / name).read_bytes()
    files['provenance/sail/source.lock.json'] = (ROOT / 'components/sail-source.lock.json').read_bytes()
    for path in (artifact / 'licenses').rglob('*'):
        if path.is_file():
            files['licenses/sail/' + str(path.relative_to(artifact / 'licenses'))] = path.read_bytes()
    files['licenses/sail/LICENSE'] = (artifact / 'LICENSE').read_bytes()
    return files, report


def validate(before, after, release, artifact, platform_artifact=None):
    """Return an exact changed-file inventory; reject unrelated payload changes."""
    expected, report = replacement_files(artifact, after['target'])
    platform = None
    if platform_artifact is not None:
        from platform_candidate import replacement_files as platform_files
        additions, platform = platform_files(platform_artifact)
        expected.update(additions)
    assert before['provenance']['sail']['version'] == report['version'], 'same-version Sail patch required'
    assert after['provenance']['sail'] == report, 'manifest Sail provenance differs from artifact'
    old, new = json.loads(json.dumps(before)), json.loads(json.dumps(after))
    old.pop('version'); new.pop('version')
    old['provenance'].pop('sail'); new['provenance'].pop('sail')
    old_files = old.pop('files'); new_files = new.pop('files')
    assert old == new, 'unrelated release metadata changed'
    assert set(old_files) <= set(new_files), 'Sail patch must preserve existing payloads'
    assert set(new_files) - set(old_files) <= set(expected), 'unreviewed payload added'
    changed = sorted(name for name in new_files if old_files.get(name) != new_files[name])
    assert any(name.startswith('python/') and name.endswith('.so') for name in changed), 'Sail binary unchanged'
    for name in changed:
        if name == 'bin/supabricks' and platform is None:
            continue  # existing native recovery exception, verified by installation verify
        assert name in expected, 'unrelated payload changed: ' + name
        assert new_files[name]['sha256'] == digest(expected[name]), name
        assert new_files[name]['executable'] == old_files.get(name, {'executable': False})['executable'], name
    # Check every installed wheel/provenance byte, even where the manifest claims
    # no change. Original .dist-info installer bookkeeping may exist separately.
    for name, data in expected.items():
        assert name in new_files, 'candidate artifact added a payload: ' + name
        assert new_files[name]['sha256'] == digest(data), name
        assert (release / name).read_bytes() == data, name
    provenance = {} if platform is None else dict(platform_commit=platform['commit'],
        platform_build_sha256=digest((platform_artifact / 'platform-build.json').read_bytes()))
    return dict(**provenance, changed_payload_files=changed, sail_commit=report['commit'],
                sail_wheel_sha256=report['wheel']['sha256'],
                sail_source_lock_sha256=report['source_lock_sha256'])
