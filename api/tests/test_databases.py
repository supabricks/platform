"""The databases domain: scenarios (L2), durability (L5) and parity (L6).

A database is a root branch and the branches made from it. Without database
engines nothing finishes, so a new database stays `provisioning`; that is
enough for the contract, names, revisions, refusals and durability. The tests
marked `engine` see databases start, suspend, resume, accept connections and
go away.

These tests run on a server of their own: an installation runs at most 32
branches, and the other suites use the shared server's.
"""
import pathlib
import re
import subprocess
import uuid

import pytest

from harness.server import Server, wait

PUBLIC_ID = re.compile(r'^db_[0-9a-f]{32}$')
MISSING = 'db_' + '0' * 32


def key():
    return f'test-{uuid.uuid4()}'


def name_for(prefix='db'):
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


@pytest.fixture(scope='module')
def own(request):
    server = Server(pathlib.Path(request.config.getoption('--binary')).resolve()).start()
    yield server
    server.stop()
    server.cleanup()


@pytest.fixture(scope='module')
def api(own):
    return own.session()


def new_project(api, name=None):
    """A project and the `main` database its apply starts a moment later."""
    made = api.call('createProject', json={'name': name or name_for('prj')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    wait(lambda: everything(api, project))
    return project


@pytest.fixture(scope='module')
def project(api):
    return new_project(api)


def everything(api, project, **params):
    """Every database of a project, following cursors."""
    items, cursor = [], None
    while True:
        page = api.call('listDatabases', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


def create(api, project, name, expect=202, used=None, **extra):
    return api.call('createDatabase', path={'project': project}, json={'name': name, **extra},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def read(api, project, database, expect=200):
    return api.call('getDatabase', path={'project': project, 'database': database}, expect=expect)


def delete(api, project, database, confirm, etag, expect=202, used=None):
    headers = {'Idempotency-Key': used or key()}
    if etag is not None:
        headers['If-Match'] = etag
    return api.call('deleteDatabase', path={'project': project, 'database': database},
                    params={} if confirm is None else {'confirm': confirm}, headers=headers, expect=expect)


def change(api, operation, project, database, expect=202, used=None):
    return api.call(operation, path={'project': project, 'database': database},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


@pytest.fixture
def fresh(api, project):
    """A database of its own for one test, as (id, name). It is deleted
    afterwards so the installation's branch limit is not used up."""
    name = name_for()
    database = create(api, project, name).json()['target']['id']
    yield database, name
    found = read(api, project, database, expect=None)
    if found.status_code == 200 and found.json()['state'] != 'deleting':
        delete(api, project, database, found.json()['name'], found.headers['ETag'], expect=None)


@pytest.fixture(scope='module')
def shared(api, project):
    """One database for tests that leave it unchanged, as (id, name)."""
    name = name_for('same')
    return create(api, project, name).json()['target']['id'], name


# ---- L2: list and get ----

@pytest.mark.l2
def test_a_new_project_has_the_database_main_which_holds_its_default_branch(api, project):
    main = next(d for d in everything(api, project) if d['name'] == 'main')
    assert PUBLIC_ID.match(main['id'])
    assert main['default_branch_id'] == 'br_' + main['id'].removeprefix('db_')
    assert main['holds_project_default'] is True
    assert main['branch_count'] == 1
    assert main['postgres_version'] == '17'
    assert main['connections'] == {'open': 0, 'limit': 64}
    assert main['state'] == 'provisioning', 'nothing starts without database engines'
    assert api.call('getProject', path={'project': project}).json()['counts']['databases'] == len(
        everything(api, project))


@pytest.mark.l2
def test_list_is_ordered_by_name_and_pages_without_gaps_or_repeats(api, shared, project):
    extra = create(api, project, name_for('aa')).json()['target']['id']
    whole = everything(api, project)
    assert [d['name'] for d in whole] == sorted(d['name'] for d in whole)
    assert len(whole) >= 3
    for limit in (1, 2):
        assert [d['id'] for d in everything(api, project, limit=limit)] == [d['id'] for d in whole]
    page = api.call('listDatabases', path={'project': project}, params={'limit': 1}).json()
    assert len(page['items']) == 1 and page['next_cursor']
    found = read(api, project, extra)
    delete(api, project, extra, found.json()['name'], found.headers['ETag'])


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'limit': '0'}, {'limit': '201'}, {'limit': 'x'}, {'cursor': 'zz'},
                                    {'cursor': '5b5d'}])
def test_list_rejects_bad_paging(api, project, params):
    response = api.call('listDatabases', path={'project': project}, params=params, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_get_returns_the_database_and_its_revision_as_etag(api, project, shared):
    database, name = shared
    response = read(api, project, database)
    body = response.json()
    assert body['id'] == database and body['name'] == name
    assert response.headers['ETag'] == f'"{body["revision"]}"'
    assert body in everything(api, project)
    assert body['created_at'].endswith('Z') and body['holds_project_default'] is False


@pytest.mark.l2
@pytest.mark.parametrize('database', [MISSING, 'main', 'db_1', str(uuid.uuid4()), 'br_' + '0' * 32])
def test_an_unknown_or_malformed_database_is_404(api, project, database):
    for operation, extra in [('getDatabase', {}), ('getDatabaseConnection', {}),
                             ('updateDatabase', dict(json={'name': 'x'})),
                             ('suspendDatabase', dict(headers={'Idempotency-Key': key()})),
                             ('revealDatabaseCredentials', dict(json={}))]:
        response = api.call(operation, path={'project': project, 'database': database}, expect=404, **extra)
        assert response.json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_database_belongs_to_one_project_only(api, project, shared):
    other = new_project(api)
    assert read(api, other, shared[0], expect=404).json()['code'] == 'not_found'
    assert shared[0] not in [d['id'] for d in everything(api, other)]
    api.call('listDatabases', path={'project': 'prj_' + '0' * 32}, expect=404)


# ---- L2: create ----

@pytest.mark.l2
def test_create_returns_an_operation_and_the_database_exists_at_once(api, project):
    used, name = key(), name_for()
    response = create(api, project, name.upper(), used=used)
    operation = response.json()
    assert operation['kind'] == 'database.create'
    assert operation['idempotency_key'] == used
    assert operation['target']['kind'] == 'database' and operation['target']['label'] == name
    assert response.headers['Location'].endswith(f"/projects/{project}/operations/{operation['id']}")
    assert operation['cancellable'] is False
    made = read(api, project, operation['target']['id']).json()
    assert made['name'] == name, 'names are stored in lower case'
    assert made['state'] == 'provisioning' and made['revision'] >= 1
    assert made['holds_project_default'] is False, 'the first database keeps the default'
    found = api.call('getOperation', path={'project': project, 'operation': operation['id']}).json()
    assert found['target'] == operation['target']
    listed = api.call('listOperations', path={'project': project},
                      params={'target': f"database:{made['id']}"}).json()['items']
    assert [o['id'] for o in listed] == [operation['id']]
    delete(api, project, made['id'], name, '*')


@pytest.mark.l2
def test_create_with_the_same_key_returns_the_first_reply(api, project):
    used, name = key(), name_for()
    first = create(api, project, name, used=used)
    again = create(api, project, name, used=used)
    assert again.json() == first.json() and again.headers['Location'] == first.headers['Location']
    assert [d['name'] for d in everything(api, project)].count(name) == 1
    assert create(api, project, name_for(), used=used, expect=409).json()['code'] == 'idempotency_key_reused'
    delete(api, project, first.json()['target']['id'], name, '*')


@pytest.mark.l2
def test_create_refuses_a_name_a_database_already_has(api, project, shared):
    for name in (shared[1], shared[1].upper(), 'main'):
        assert create(api, project, name, expect=409).json()['code'] == 'name_taken'


@pytest.mark.l2
def test_the_same_name_can_be_used_in_another_project(api, project, shared):
    other = new_project(api)
    made = create(api, other, shared[1]).json()['target']['id']
    assert read(api, other, made).json()['name'] == shared[1]
    delete(api, other, made, shared[1], '*')


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'name': ''}, {'name': 'has space'}, {'name': '-lead'}, {'name': 'a' * 41},
                                  {'name': 7}, {'name': 'ok', 'compute_size': 's'}, [], 'text'])
def test_create_rejects_an_invalid_body(api, project, body):
    before = len(everything(api, project))
    response = api.call('createDatabase', path={'project': project}, json=body,
                        headers={'Idempotency-Key': key()}, expect=400)
    assert response.json()['code'] == 'invalid_request'
    assert len(everything(api, project)) == before


@pytest.mark.l2
@pytest.mark.parametrize('headers', [{}, {'Idempotency-Key': 'short'}, {'Idempotency-Key': 'has space in it'}])
def test_create_requires_a_well_formed_idempotency_key(api, project, headers):
    response = api.call('createDatabase', path={'project': project}, json={'name': name_for()},
                        headers=headers, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_an_installation_runs_at_most_32_branches(request):
    server = Server(pathlib.Path(request.config.getoption('--binary')).resolve()).start()
    try:
        api = server.session()
        project = new_project(api)
        for index in range(31):
            create(api, project, f'fill-{index}')
        refused = create(api, project, 'one-too-many', expect=409).json()
        assert refused['code'] == 'capacity_exhausted'
        assert len(everything(api, project)) == 32
    finally:
        server.stop()
        server.cleanup()


# ---- L2: rename ----

def rename(api, project, database, name, expect=200, etag=None):
    return api.call('updateDatabase', path={'project': project, 'database': database}, json={'name': name},
                    headers={'If-Match': etag} if etag else {}, expect=expect)


@pytest.mark.l2
def test_rename_keeps_the_id_and_raises_the_revision(api, project, fresh):
    database, name = fresh
    before = read(api, project, database).json()
    renamed = name_for('renamed')
    response = rename(api, project, database, renamed.upper())
    after = response.json()
    assert after['id'] == database and after['name'] == renamed
    assert after['revision'] == before['revision'] + 1
    assert response.headers['ETag'] == f'"{after["revision"]}"'
    assert after['created_at'] == before['created_at'] and after['state'] == before['state']
    assert read(api, project, database).json() == after
    assert create(api, project, name).status_code == 202, 'the old name is free'


@pytest.mark.l2
def test_a_rename_to_the_same_name_changes_nothing(api, project, shared):
    before = read(api, project, shared[0]).json()
    assert rename(api, project, shared[0], shared[1]).json() == before


@pytest.mark.l2
def test_rename_to_a_name_in_use_is_refused(api, project, shared):
    before = read(api, project, shared[0]).json()
    assert rename(api, project, shared[0], 'main', expect=409).json()['code'] == 'name_taken'
    assert read(api, project, shared[0]).json() == before


@pytest.mark.l2
def test_if_match_makes_a_rename_conditional(api, project, fresh):
    database = fresh[0]
    etag = read(api, project, database).headers['ETag']
    rename(api, project, database, name_for('first'), etag=etag)
    stale = rename(api, project, database, name_for('second'), etag=etag, expect=412)
    assert stale.json()['code'] == 'revision_mismatch'
    assert rename(api, project, database, name_for('x'), etag='nonsense', expect=412).json()['code'] == 'revision_mismatch'
    rename(api, project, database, name_for('third'), etag='*')
    rename(api, project, database, name_for('fourth'), etag=read(api, project, database).headers['ETag'])


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'name': ''}, {'name': 'has space'}, {'name': 'ok', 'compute_size': 's'},
                                  {'default_branch_id': 'br_' + '0' * 32}, []])
def test_rename_rejects_an_invalid_body(api, project, shared, body):
    before = read(api, project, shared[0]).json()
    response = api.call('updateDatabase', path={'project': project, 'database': shared[0]}, json=body, expect=400)
    assert response.json()['code'] == 'invalid_request'
    assert read(api, project, shared[0]).json() == before


# ---- L2: suspend and resume ----

@pytest.mark.l2
def test_suspend_returns_an_operation_and_the_state_follows(api, project, fresh):
    database = fresh[0]
    before = read(api, project, database).json()
    used = key()
    response = change(api, 'suspendDatabase', project, database, used=used)
    operation = response.json()
    assert operation['kind'] == 'database.suspend' and operation['target']['id'] == database
    assert operation['idempotency_key'] == used
    after = read(api, project, database).json()
    assert after['state'] == 'suspending' and after['revision'] == before['revision'] + 1
    assert change(api, 'suspendDatabase', project, database, used=used).json() == operation, 'a repeat is the same reply'
    assert change(api, 'suspendDatabase', project, database, expect=409).json()['code'] == 'conflict'
    assert read(api, project, database).json() == after


@pytest.mark.l2
def test_resume_waits_for_an_earlier_change(api, project, fresh):
    change(api, 'suspendDatabase', project, fresh[0])
    refused = change(api, 'resumeDatabase', project, fresh[0], expect=409).json()
    assert refused['code'] == 'conflict' and 'in progress' in refused['detail']


@pytest.mark.l2
@pytest.mark.parametrize('operation', ['suspendDatabase', 'resumeDatabase'])
def test_a_state_change_requires_an_idempotency_key(api, project, shared, operation):
    response = api.call(operation, path={'project': project, 'database': shared[0]}, expect=400)
    assert response.json()['code'] == 'invalid_request'


# ---- L2: delete ----

@pytest.mark.l2
def test_delete_returns_an_operation_and_the_database_shows_deleting(api, project, fresh):
    database, name = fresh
    etag = read(api, project, database).headers['ETag']
    operation = delete(api, project, database, name, etag).json()
    assert operation['kind'] == 'database.delete' and operation['target']['id'] == database
    found = read(api, project, database).json()
    assert found['state'] == 'deleting'
    assert database in [d['id'] for d in everything(api, project)]
    assert create(api, project, name, expect=409).json()['code'] == 'name_taken', 'the name is free when it is gone'
    for attempt in ('suspendDatabase', 'resumeDatabase'):
        assert change(api, attempt, project, database, expect=409).json()['code'] == 'conflict'
    assert rename(api, project, database, name_for(), expect=409).json()['code'] == 'conflict'
    assert delete(api, project, database, name, '*', expect=409).json()['code'] == 'conflict'


@pytest.mark.l2
def test_delete_needs_the_current_version_and_the_exact_name(api, project, shared):
    database, name = shared
    etag = read(api, project, database).headers['ETag']
    assert delete(api, project, database, name, None, expect=428).json()['code'] == 'precondition_required'
    assert delete(api, project, database, name, '"999"', expect=412).json()['code'] == 'revision_mismatch'
    for wrong in (None, '', name.upper(), name + 'x'):
        assert delete(api, project, database, wrong, etag, expect=400).json()['code'] == 'invalid_request'
    response = api.call('deleteDatabase', path={'project': project, 'database': database},
                        params={'confirm': name}, headers={'If-Match': etag}, expect=400)
    assert response.json()['code'] == 'invalid_request', 'an Idempotency-Key is required'
    assert read(api, project, database).json()['state'] == 'provisioning'


@pytest.mark.l2
def test_delete_with_the_same_key_returns_the_first_reply(api, project, fresh):
    database, name = fresh
    used, etag = key(), read(api, project, database).headers['ETag']
    first = delete(api, project, database, name, etag, used=used)
    again = delete(api, project, database, name, etag, used=used)
    assert again.json() == first.json()


@pytest.mark.l2
def test_the_database_that_holds_the_default_branch_is_not_deleted_alone(api, project):
    main = next(d for d in everything(api, project) if d['name'] == 'main')
    refused = delete(api, project, main['id'], 'main', '*', expect=409).json()
    assert refused['code'] == 'delete_blocked' and 'default' in refused['detail']
    assert read(api, project, main['id']).json() == main


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def runtime_of(server, database):
    """The daemon's own ID for the project a database is in, as the command line uses it."""
    return server.rpc(method='branch', id=str(uuid.UUID(database.removeprefix('db_'))))['branch']['project_id']


# ---- L2: connecting ----

@pytest.mark.l2
def test_connection_details_need_the_engines(api, project, shared):
    for operation, extra in [('getDatabaseConnection', {}), ('revealDatabaseCredentials', dict(json={})),
                             ('revealDatabaseCredentials', {})]:
        response = api.call(operation, path={'project': project, 'database': shared[0]}, expect=503, **extra)
        assert response.json()['code'] == 'unavailable'


@pytest.mark.l2
def test_a_connection_names_a_branch_of_the_same_database(api, project, shared):
    main = next(d for d in everything(api, project) if d['name'] == 'main')
    path = {'project': project, 'database': shared[0]}
    for branch in (main['default_branch_id'], 'br_' + '0' * 32, 'nonsense'):
        response = api.call('getDatabaseConnection', path=path, params={'branch': branch}, expect=404)
        assert response.json()['code'] == 'not_found'
        api.call('revealDatabaseCredentials', path=path, json={'branch_id': branch}, expect=404)
    api.call('revealDatabaseCredentials', path=path, json={'role': 'postgres'}, expect=400)


# ---- L5: durability ----

@pytest.mark.l5
def test_databases_and_replies_survive_a_killed_daemon(private_server):
    api = private_server.session()
    project = new_project(api)
    used, name = key(), name_for()
    first = create(api, project, name, used=used).json()
    database = first['target']['id']
    renamed = rename(api, project, database, name_for('kept')).json()
    suspended, suspend_key = None, key()
    suspended = change(api, 'suspendDatabase', project, database, used=suspend_key).json()
    before = read(api, project, database).json()
    private_server.restart(kill=True)
    api = private_server.session()
    assert read(api, project, database).json() == before
    assert before['name'] == renamed['name'] and before['state'] == 'suspending'
    assert create(api, project, name, used=used).json() == first, 'the key still returns the first reply'
    assert change(api, 'suspendDatabase', project, database, used=suspend_key).json() == suspended
    assert [d['id'] for d in everything(api, project)].count(database) == 1


# ---- L6: parity with the command line ----

@pytest.mark.l6
def test_the_api_and_the_command_line_see_the_same_databases(api, own, project, fresh):
    database, name = fresh
    runtime = runtime_of(own, database)
    branches = own.rpc(method='list_branches', project_id=runtime, include_deleted=False)
    roots = {b['branch']['id'].replace('-', ''): b for b in branches if b['branch']['parent_id'] is None}
    listed = {d['id'].removeprefix('db_'): d for d in everything(api, project) if d['state'] != 'deleting'}
    assert set(roots) == set(listed)
    for identity, record in roots.items():
        assert listed[identity]['name'] == record['branch']['name']
        assert listed[identity]['holds_project_default'] == record['is_default']
    made = own.rpc(method='submit', project_id=runtime, key=key(), mutation=dict(
        kind='create_database', name=name_for('cli'),
        ports=dict(sql=free_port(), external_http=free_port(), internal_http=free_port())))
    theirs = 'db_' + made['branch_id'].replace('-', '')
    found = read(api, project, theirs).json()
    assert found['state'] == 'provisioning'
    operations = api.call('listOperations', path={'project': project},
                          params={'target': f'database:{theirs}'}).json()['items']
    assert [o['kind'] for o in operations] == ['database.create']
    own.rpc(method='rename_branch', project_id=runtime, id=made['branch_id'], name=name_for('theirs'))
    assert read(api, project, theirs).json()['revision'] == found['revision'] + 1
    delete(api, project, theirs, read(api, project, theirs).json()['name'], '*')


# ---- With database engines: work that finishes ----

def finished(api, project, operation, timeout=180):
    def final():
        found = api.call('getOperation', path={'project': project, 'operation': operation['id']}, expect=200).json()
        return found if found['finished_at'] else None
    done = wait(final, timeout=timeout)
    assert done['state'] == 'succeeded', done
    return done


def psql(request, uri, sql):
    binary = pathlib.Path(request.config.getoption('--bundle')) / 'pg_install/v17/bin/psql'
    result = subprocess.run([str(binary), uri, '-qAt', '-c', sql], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def idle(api, project, database):
    """Wait until the server has seen every client connection close: a database
    with one open is, correctly, not suspended or deleted."""
    wait(lambda: read(api, project, database).json()['connections']['open'] == 0)


@pytest.mark.engine
@pytest.mark.l2
def test_a_database_starts_accepts_connections_suspends_resumes_and_goes(engine_api, engine_server, request):
    api = engine_api
    made = api.call('createProject', json={'name': name_for('live')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    main = next(d for d in everything(api, project) if d['name'] == 'main')
    assert main['state'] == 'running' and main['holds_project_default'] is True

    name = name_for('second')
    operation = create(api, project, name).json()
    database = operation['target']['id']
    done = finished(api, project, operation)
    assert done['result']['database']['id'] == database
    assert all(step['state'] == 'succeeded' for step in done['steps'])
    assert read(api, project, database).json()['state'] == 'running'

    path = {'project': project, 'database': database}
    connection = api.call('getDatabaseConnection', path=path, expect=200).json()
    assert connection['available'] is True and connection['branch_id'] == 'br_' + database.removeprefix('db_')
    assert connection['host'] == '127.0.0.1' and connection['database'] == 'postgres'
    assert 'password' not in connection
    assert api.call('getDatabaseConnection', path=path).json() == connection, 'the address is stable'
    other = api.call('getDatabaseConnection', path={'project': project, 'database': main['id']}).json()
    assert other['port'] != connection['port'], 'each database has its own address'
    revealed = api.call('revealDatabaseCredentials', path=path, json={}, expect=200)
    assert revealed.headers['Cache-Control'] == 'no-store'
    secret = revealed.json()
    assert secret['role'] == connection['role'] and secret['expires_at'] is None
    assert f":{connection['port']}/" in secret['uri'] and secret['password'] in secret['uri']
    assert psql(request, secret['uri'], 'create table t as select 41 + 1 as answer; select answer from t') == '42'
    assert psql(request, secret['uri'], 'select current_user') == connection['role']

    idle(api, project, database)
    done = finished(api, project, change(api, 'suspendDatabase', project, database).json())
    assert done['kind'] == 'database.suspend'
    assert read(api, project, database).json()['state'] == 'suspended'
    assert read(api, project, main['id']).json()['state'] == 'running', 'other databases are not affected'
    assert change(api, 'suspendDatabase', project, database, expect=409).json()['code'] == 'conflict'
    done = finished(api, project, change(api, 'resumeDatabase', project, database).json())
    assert done['kind'] == 'database.resume'
    assert read(api, project, database).json()['state'] == 'running'
    assert psql(request, secret['uri'], 'select answer from t') == '42', 'data survives a suspend'
    again = finished(api, project, change(api, 'resumeDatabase', project, database).json())
    assert again['steps'] == [], 'resuming a running database changes nothing'

    idle(api, project, database)
    renamed = rename(api, project, database, name_for('renamed')).json()
    assert api.call('getDatabaseConnection', path=path).json() == connection, 'a rename keeps the address'

    # Branches are made on the command line until the branches domain is served.
    runtime, root = runtime_of(engine_server, database), str(uuid.UUID(database.removeprefix('db_')))
    child = engine_server.rpc(method='submit', project_id=runtime, key=key(), mutation=dict(
        kind='create_branch', name=name_for('child'), parent_id=root,
        ports=dict(sql=free_port(), external_http=free_port(), internal_http=free_port())))
    finished(api, project, dict(id='op_' + child['id'].replace('-', '')))
    assert read(api, project, database).json()['branch_count'] == 2
    branch = 'br_' + child['branch_id'].replace('-', '')
    theirs = api.call('getDatabaseConnection', path=path, params={'branch': branch}, expect=200).json()
    assert theirs['branch_id'] == branch and theirs['port'] != connection['port']
    refused = delete(api, project, database, renamed['name'], '*', expect=409).json()
    assert refused['code'] == 'delete_blocked' and 'branches' in refused['detail']
    gone = engine_server.rpc(method='submit', project_id=runtime, key=key(), mutation=dict(
        kind='set_state', branch_id=child['branch_id'], expected_revision=child['revision'], desired='deleted'))
    finished(api, project, dict(id='op_' + gone['id'].replace('-', '')))
    assert read(api, project, database).json()['branch_count'] == 1
    found = read(api, project, database)
    finished(api, project, delete(api, project, database, renamed['name'], found.headers['ETag']).json())
    assert read(api, project, database, expect=404).json()['code'] == 'not_found'
    assert database not in [d['id'] for d in everything(api, project)]
    finished(api, project, create(api, project, renamed['name']).json())


@pytest.mark.engine
@pytest.mark.l2
def test_connecting_wakes_a_suspended_database(engine_api, request):
    api = engine_api
    made = api.call('createProject', json={'name': name_for('wake')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    database = finished(api, project, create(api, project, name_for('sleepy')).json())['target']['id']
    path = {'project': project, 'database': database}
    uri = api.call('revealDatabaseCredentials', path=path, expect=200).json()['uri']
    finished(api, project, change(api, 'suspendDatabase', project, database).json())
    assert api.call('getDatabaseConnection', path=path, expect=200).json()['available'] is True
    assert read(api, project, database).json()['state'] == 'suspended', 'reading the address does not wake it'
    assert psql(request, uri, 'select 1') == '1'
    assert read(api, project, database).json()['state'] == 'running'


# --- The same operations for signed-in callers on a governed server -----------

@pytest.mark.l2
@pytest.mark.parametrize('caller', ['viewer', 'editor', 'administrator'])
def test_a_signed_in_caller_reads_the_databases_of_their_project(caller, governed):
    listed = governed.callers[caller].call('listDatabases', path=dict(project=governed.project), expect=200).json()
    assert governed.database in [d['id'] for d in listed['items']]
    found = governed.callers[caller].call(
        'getDatabase', path=dict(project=governed.project, database=governed.database), expect=200).json()
    assert found['name'] == 'roles-db'


@pytest.mark.l2
def test_a_database_in_another_project_is_not_revealed(governed):
    path = dict(project=governed.other_project, database=governed.database)
    assert governed.callers['elsewhere'].call('getDatabase', path=path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_governed_server_does_not_change_databases_yet(governed):
    admin, one = governed.callers['administrator'], dict(project=governed.project, database=governed.database)
    before = admin.call('getDatabase', path=one, expect=200).json()
    attempts = [('createDatabase', dict(path=dict(project=governed.project), json={'name': 'more'})),
                ('updateDatabase', dict(path=one, json={'name': 'renamed'})),
                ('deleteDatabase', dict(path=one, params={'confirm': 'roles-db'}, headers={'If-Match': '*'})),
                ('suspendDatabase', dict(path=one)), ('resumeDatabase', dict(path=one)),
                ('revealDatabaseCredentials', dict(path=one, json={}))]
    for operation, arguments in attempts:
        arguments.setdefault('headers', {})['Idempotency-Key'] = key()
        refused = admin.call(operation, expect=403, **arguments).json()
        assert refused['code'] == 'unavailable_in_profile', operation
    assert admin.call('getDatabase', path=one, expect=200).json() == before


@pytest.mark.l2
def test_nobody_connects_directly_on_a_governed_server(governed):
    path = dict(project=governed.project, database=governed.database)
    for caller in ('viewer', 'administrator'):
        found = governed.callers[caller].call('getDatabaseConnection', path=path, expect=200).json()
        assert found == {'available': False, 'branch_id': 'br_' + governed.database.removeprefix('db_')}
