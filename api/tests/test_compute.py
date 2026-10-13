"""Compute, idle lifecycle and storage: HTTP contract plus a native resize."""
import pytest
from test_databases import (api, own, project, key, name_for, create, read, everything,
                            finished, psql, idle)
from harness.server import wait


@pytest.mark.l2
def test_compute_catalog_and_atomic_creation(api, project):
    catalog = api.call('listComputeSizes', expect=200).json()
    assert catalog['cpu_memory_isolation'] is False
    assert catalog['default'] == 'm'
    assert [p['id'] for p in catalog['items']] == ['s', 'm', 'l']
    assert [p['max_connections'] for p in catalog['items']] == [32, 64, 128]
    used, name = key(), name_for('sized')
    response = create(api, project, name, used=used, compute_size='s', idle_timeout_seconds=120)
    database = response.json()['target']['id']
    view = read(api, project, database).json()
    assert view['compute']['size'] == 's'
    assert view['compute']['idle_timeout_seconds'] == 120
    assert view['connections'] == {'open': 0, 'limit': 32}
    assert view['storage']['status'] == 'unavailable'
    assert view['storage']['logical_bytes'] is None
    assert view['storage']['database']['total_physical_bytes'] is None
    assert create(api, project, name, used=used, compute_size='s', idle_timeout_seconds=120).json() == response.json()
    create(api, project, name, used=used, compute_size='s', idle_timeout_seconds=None, expect=409)


@pytest.mark.l2
@pytest.mark.parametrize('extra', [dict(compute_size='xl'),dict(compute_size=None),
                                  dict(idle_timeout_seconds=0),dict(idle_timeout_seconds=True),
                                  dict(idle_timeout_seconds=86401),dict(idle_timeout_seconds=1.5)])
def test_compute_creation_rejects_bad_settings(api, project, extra):
    create(api, project, name_for(), expect=400, **extra)


@pytest.mark.l2
def test_compute_change_requires_explicit_revision_and_separate_fields(api, project):
    database = everything(api, project)[0]['id']
    path = dict(project=project, database=database)
    api.call('updateDatabase', path=path, json={'compute_size': 's'},
             headers={'Idempotency-Key': key()}, expect=428)
    api.call('updateDatabase', path=path, json={'compute_size': 's'},
             headers={'Idempotency-Key': key(), 'If-Match': '*'}, expect=400)
    api.call('updateDatabase', path=path, json={'compute_size': 's', 'name': 'mixed'}, expect=400)


@pytest.mark.engine
@pytest.mark.l2
def test_native_resize_preserves_data_and_idle_suspend_wakes_on_connection(engine_api, request):
    api = engine_api
    made = api.call('createProject', json={'name': name_for('compute')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    op = create(api, project, name_for('resizable'), compute_size='s').json()
    database = op['target']['id']
    finished(api, project, op)
    path = dict(project=project, database=database)
    connection = api.call('getDatabaseConnection', path=path, expect=200).json()
    uri = api.call('revealDatabaseCredentials', path=path, json={}, expect=200).json()['uri']
    assert psql(request, uri, 'create table retained as select 42 as value; show max_connections') == '48'
    idle(api, project, database)
    etag = read(api, project, database).headers['ETag']
    api.call('updateDatabase', path=path, json={'compute_size': 'm'},
             headers={'Idempotency-Key': key(), 'If-Match': etag}, expect=409)
    headers = {'Idempotency-Key': key(), 'If-Match': etag}
    body = {'compute_size': 'm', 'allow_restart': True}
    accepted = api.call('updateDatabase', path=path, json=body, headers=headers, expect=202).json()
    finished(api, project, accepted)
    assert api.call('updateDatabase', path=path, json=body, headers=headers, expect=202).json() == accepted
    assert api.call('getDatabaseConnection', path=path, expect=200).json() == connection
    assert psql(request, uri, 'select value from retained; show max_connections') == '42\n80'
    view = read(api, project, database)
    assert view.json()['connections']['limit'] == 64
    api.call('updateDatabase', path=path, json={'idle_timeout_seconds': 60},
             headers={'Idempotency-Key': key(), 'If-Match': view.headers['ETag']}, expect=202)
    wait(lambda: read(api, project, database).json()['storage']['status'] == 'fresh', timeout=100)
    storage = read(api, project, database).json()['storage']
    assert storage['logical_bytes'] > 0
    assert storage['exclusive_physical_bytes'] >= 0
    assert storage['database']['total_physical_bytes'] == storage['exclusive_physical_bytes']
    wait(lambda: read(api, project, database).json()['state'] == 'suspended', timeout=100)
    suspended = read(api, project, database).json()
    assert suspended['compute']['last_suspend_reason'] == 'idle_timeout'
    assert psql(request, uri, 'select value from retained') == '42'
    assert read(api, project, database).json()['state'] == 'running'


@pytest.mark.engine
@pytest.mark.l2
def test_native_branch_creation_and_configuration_use_the_selected_profile(engine_api, request):
    api = engine_api
    made = api.call('createProject', json={'name': name_for('branch-size')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    parent = everything(api, project)[0]
    path = dict(project=project, database=parent['id'])
    uri = api.call('revealDatabaseCredentials', path=path, json={}, expect=200).json()['uri']
    assert psql(request, uri, 'create table inherited as select 17 as value; select value from inherited') == '17'
    idle(api, project, parent['id'])
    made = api.call('createBranch', path=path,
                    json={'name': name_for('small'), 'compute_size': 's', 'idle_timeout_seconds': 120},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    finished(api, project, made)
    child = {**path, 'branch': made['target']['id']}
    view = api.call('getBranch', path=child, expect=200)
    assert view.json()['compute']['size'] == 's'
    assert view.json()['connections']['limit'] == 32
    uri = api.call('revealDatabaseCredentials', path=path, json={'branch_id': child['branch']}, expect=200).json()['uri']
    assert psql(request, uri, 'select value from inherited; show shared_buffers; show max_connections') == '17\n256MB\n48'
    headers = {'Idempotency-Key': key(), 'If-Match': view.headers['ETag']}
    changed = api.call('updateBranch', path=child, json={'idle_timeout_seconds': None}, headers=headers, expect=202).json()
    assert changed['state'] == 'succeeded'
    assert changed['kind'] == 'branch.configure_compute'
    assert api.call('getBranch', path=child).json()['compute']['idle_timeout_seconds'] is None
    assert read(api, project, parent['id']).json()['compute']['size'] == 'm'
