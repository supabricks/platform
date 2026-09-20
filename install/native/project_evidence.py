"""PK07 validation for R04: exact shared artifact and target closure provenance."""
import hashlib
import json
import math
from project_portability import CHECKS, TARGETS


def require(value, message):
    if not value:
        raise ValueError('project portability: '+message)


def sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def collect(directory, targets):
    source = directory / 'project-source/producer.json'
    producer = json.loads(source.read_text())
    artifact = directory / 'project-source/sales.sbproj'
    with artifact.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    require(producer.get('status') == 'passed' and actual == producer.get('package_sha256'), 'shared source artifact missing or changed')
    require(producer.get('producer_target') == 'linux-x86_64', 'unexpected producer target')
    for key in ('package_sha256', 'content_sha256', 'source_sha256', 'release_sha256'):
        require(sha(producer.get(key)), 'missing source identity '+key)
    native = targets['linux-x86_64']
    require(producer['producer_archive'] == native['archive'] and producer['release_sha256'] == native['release_sha256'], 'producer archive differs')
    require(set(producer.get('closures', {})) == set(TARGETS), 'missing native closures')
    env = producer['environment']
    for key in ('pyproject_sha256', 'lock_sha256'):
        require(sha(env.get(key)), 'missing environment declaration hash')
    require(set(env.get('bundles', {})) == set(TARGETS), 'missing inspected closures')
    require(set(producer.get('fixtures', {})) == {
        'fixtures/sales.csv', 'migrations/001-marker.sql', 'migrations/002-marker.sql',
        'notebooks/sales.ipynb', 'queries/sales_total.sql'}, 'fixture inventory differs')
    require(all(sha(h) for h in producer['fixtures'].values()), 'invalid fixture hashes')
    require(producer['measurements']['archive_bytes'] == artifact.stat().st_size, 'artifact size differs')
    require(producer['measurements']['rows'] == 2, 'fixture size differs')
    data_artifact=directory/'project-source/sales.sbdata'
    logical=producer['logical_data']
    require(logical.get('verified') is True and logical.get('profile')=='postgres_tables'
            and sha(logical.get('content_sha256')) and logical.get('archive_sha256')==hashlib.sha256(data_artifact.read_bytes()).hexdigest(), 'logical data artifact differs')
    require(producer['closures']['linux-x86_64'].get('logical_data')==logical, 'logical data producer provenance differs')
    result = {}
    for target in TARGETS:
        native = targets[target]
        closure = producer['closures'][target]
        require(closure.get('status') == 'passed' and closure.get('target') == target, 'failed native closure export')
        require(closure['archive'] == native['archive'] and closure['release_sha256'] == native['release_sha256'], 'mixed closure archive')
        manifest = closure['manifest']
        require(manifest['version'] == 1 and manifest['target'] == target, 'closure target mismatch')
        require(manifest['contract'] == native['kernel_contract_sha256'], 'closure kernel contract differs')
        require(manifest['files']['pyproject.toml'] == env['pyproject_sha256'] and manifest['files']['uv.lock'] == env['lock_sha256'], 'closure declarations differ')
        require(all(sha(h) for h in manifest['files'].values()), 'invalid wheel hashes')
        wheels = {n:h for n,h in manifest['files'].items() if n.startswith('wheels/') and n.endswith('.whl')}
        require(wheels and len(wheels)+2 == len(manifest['files']), 'invalid wheel closure inventory')
        inspected = env['bundles'][target]
        require(sha(closure['bundle_sha256']) and inspected['sha256'] == closure['bundle_sha256']
                and inspected['kernel_contract'] == manifest['contract'] and inspected['wheels'] == len(wheels), 'inspected closure differs')
        path = directory / f'release-projects-{target}/projects.json'
        data = json.loads(path.read_text())
        require(data.get('status') == 'passed' and not any(data.get(k) for k in ('error', 'errors', 'cleanup_failed', 'cleanup_errors', 'measurement_failed')), 'failed suite or cleanup')
        require(set(data.get('checks', [])) == set(CHECKS) and len(data['checks']) == len(CHECKS), 'incomplete checks')
        require(data.get('target') == target and data.get('archive') == native['archive'] and data.get('release_sha256') == native['release_sha256'], 'mixed destination archive')
        require(data.get('producer') == producer, 'consumers used different source artifacts')
        require(data.get('logical_data')==dict(archive_sha256=logical['archive_sha256'],content_sha256=logical['content_sha256'],rows=2), 'incomplete logical data transfer')
        previous = data['previous_archive']
        require(previous.get('version') == 'v0.1.0-alpha.22' and previous.get('target') == target and sha(previous.get('sha256')), 'missing predecessor identity')
        require(data.get('network_evidence') and 'not isolated' not in data['network_evidence'], 'missing network isolation')
        measurements = {}
        for name in ('archive_bytes', 'unpacked_bytes', 'rows', 'prepare_seconds', 'start_seconds', 'peak_rss_bytes', 'disk_peak_bytes'):
            value = data['measurements'].get(name)
            require(type(value) in (int, float) and math.isfinite(value) and value > 0, 'missing measurement '+name)
            measurements[name] = value
        require(all(measurements[n] == producer['measurements'][n] for n in ('archive_bytes', 'unpacked_bytes', 'rows')), 'fixture measurements differ')
        result[target] = dict(package_sha256=actual, source_sha256=producer['source_sha256'], content_sha256=producer['content_sha256'],
                              environment=env, fixtures=producer['fixtures'], measurements=measurements, logical_data=data['logical_data'],
                              network=data['network_evidence'], previous_archive=previous,
                              reports={str(path.relative_to(directory)):dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), checks=len(CHECKS)),
                                       str(source.relative_to(directory)):dict(sha256=hashlib.sha256(source.read_bytes()).hexdigest(), checks=1)})
    return result
