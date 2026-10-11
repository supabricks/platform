"""The branches domain: scenarios (L2), durability (L5) and parity (L6).

A branch is made from another branch of its database. Without database
engines a database's first branch never finishes starting, and the platform
does not branch from one that is still changing, so here the engineless tests
cover the first branch, every refusal and the contract; the tests marked
`engine` make real branches, at the head, at a time and at a log position,
and see them diverge, expire and go away.

These tests run on a server of their own, like the database tests.
"""
import datetime
import re
import time
import uuid

import pytest

from harness.server import wait
from test_databases import (api, create as create_database, delete as delete_database, everything as databases,  # noqa: F401
                            finished, idle, key, name_for, new_project, own, project, psql,
                            read as read_database, runtime_of)

PUBLIC_ID = re.compile(r'^br_[0-9a-f]{32}$')
MISSING = 'br_' + '0' * 32


def first(database):
    """The ID of a database's first branch."""
    return 'br_' + database.removeprefix('db_')


def later(seconds=3600):
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def everything(api, project, database, **params):
    """Every branch of a database, following cursors."""
    items, cursor = [], None
    while True:
        page = api.call('listBranches', path={'project': project, 'database': database},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


def at(project, database, branch):
    return {'project': project, 'database': database, 'branch': branch}


def create(api, project, database, name, expect=202, used=None, **extra):
    return api.call('createBranch', path={'project': project, 'database': database}, json={'name': name, **extra},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def read(api, project, database, branch, expect=200):
    return api.call('getBranch', path=at(project, database, branch), expect=expect)


def update(api, project, database, branch, body, etag=None, expect=200):
    return api.call('updateBranch', path=at(project, database, branch), json=body,
                    headers={'If-Match': etag} if etag else {}, expect=expect)


def delete(api, project, database, branch, etag='*', expect=202, used=None):
    headers = {'Idempotency-Key': used or key()}
    if etag is not None:
        headers['If-Match'] = etag
    return api.call('deleteBranch', path=at(project, database, branch), headers=headers, expect=expect)


def change(api, operation, project, database, branch, expect=202, used=None):
    return api.call(operation, path=at(project, database, branch),
                    headers={'Idempotency-Key': used or key()}, expect=expect)


@pytest.fixture
def fresh(api, project):
    """A database of its own for one test, as (database, first branch). It is
    deleted afterwards so the installation's branch limit is not used up."""
    database = create_database(api, project, name_for()).json()['target']['id']
    yield database, first(database)
    found = read_database(api, project, database, expect=None)
    if found.status_code == 200 and found.json()['state'] != 'deleting':
        if found.json()['holds_project_default']:
            main = next(d for d in databases(api, project) if d['name'] == 'main')
            api.call('setDefaultBranch', path=at(project, main['id'], first(main['id'])), expect=200)
        delete_database(api, project, database, found.json()['name'], '*', expect=None)


@pytest.fixture(scope='module')
def shared(api, project):
    """One database for tests that leave it unchanged, as (database, first branch, name)."""
    name = name_for('same')
    database = create_database(api, project, name).json()['target']['id']
    return database, first(database), name


# ---- L2: list and get ----

@pytest.mark.l2
def test_a_database_has_a_first_branch_with_its_name_and_no_parent(api, project, shared):
    database, branch, name = shared
    listed = everything(api, project, database)
    assert [b['id'] for b in listed] == [branch]
    found = listed[0]
    assert PUBLIC_ID.match(found['id'])
    assert found['database_id'] == database and found['name'] == name
    assert found['parent_id'] is None and found['created_from'] is None
    assert found['state'] == 'provisioning', 'nothing starts without database engines'
    assert found['is_default'] is False and found['expires_at'] is None
    assert found['child_count'] == 0 and found['connections'] == {'open': 0, 'limit': 64}
    assert read_database(api, project, database).json()['default_branch_id'] == branch
    one = read(api, project, database, branch)
    assert one.json() == found and one.headers['ETag'] == f'"{found["revision"]}"'


@pytest.mark.l2
def test_the_first_branch_of_main_is_the_projects_default(api, project):
    main = next(d for d in databases(api, project) if d['name'] == 'main')
    assert read(api, project, main['id'], first(main['id'])).json()['is_default'] is True


@pytest.mark.l2
def test_a_branch_is_found_only_under_its_own_database(api, project, shared):
    database, branch, _ = shared
    main = next(d for d in databases(api, project) if d['name'] == 'main')
    assert read(api, project, main['id'], branch, expect=404).json()['code'] == 'not_found'
    for unknown in (MISSING, 'nonsense', database):
        assert read(api, project, database, unknown, expect=404).json()['code'] == 'not_found'
    path = {'project': project, 'database': 'db_' + '0' * 32}
    assert api.call('listBranches', path=path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_list_refuses_a_cursor_it_did_not_issue(api, project, shared):
    path = {'project': project, 'database': shared[0]}
    assert api.call('listBranches', path=path, params={'cursor': 'nonsense'}, expect=400).json()['code'] == 'invalid_request'


# ---- L2: create ----

@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'name': ''}, {'name': 'has space'}, {'name': 'x' * 41}, {'name': 7},
    {'name': 'ok', 'unknown': 1}, {'name': 'ok', 'start_suspended': True},
    {'name': 'ok', 'from': 'head'}, {'name': 'ok', 'from': {'kind': 'tag'}},
    {'name': 'ok', 'from': {'kind': 'time'}}, {'name': 'ok', 'from': {'kind': 'lsn', 'lsn': 'nonsense'}},
    {'name': 'ok', 'from': {'kind': 'head', 'extra': 1}},
    {'name': 'ok', 'expires_at': 'tomorrow'}, {'name': 'ok', 'expires_at': '2001-01-01T00:00:00Z'},
    {'name': 'ok', 'expires_at': 5},
])
def test_create_refuses_a_malformed_request(api, project, shared, body):
    response = api.call('createBranch', path={'project': project, 'database': shared[0]}, json=body,
                        headers={'Idempotency-Key': key()}, expect=400)
    assert response.json()['code'] == 'invalid_request'
    assert len(everything(api, project, shared[0])) == 1


@pytest.mark.l2
def test_create_requires_an_idempotency_key(api, project, shared):
    response = api.call('createBranch', path={'project': project, 'database': shared[0]},
                        json={'name': name_for()}, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_create_refuses_a_name_a_database_or_branch_has(api, project, shared):
    database, _, name = shared
    for taken in (name, name.upper(), 'main'):
        refused = create(api, project, database, taken, expect=409).json()
        assert refused['code'] == 'name_taken', taken


@pytest.mark.l2
def test_create_refuses_a_parent_that_is_not_a_branch_of_the_database(api, project, shared):
    database, _, _ = shared
    main = next(d for d in databases(api, project) if d['name'] == 'main')
    for parent in (MISSING, first(main['id']), 'nonsense'):
        refused = create(api, project, database, name_for(), parent_id=parent, expect=404).json()
        assert refused['code'] == 'not_found', parent


@pytest.mark.l2
def test_create_waits_for_the_branch_it_starts_from(api, project, shared):
    database, branch, _ = shared
    for extra in ({}, {'parent_id': branch}, {'from': {'kind': 'head'}}):
        refused = create(api, project, database, name_for(), expect=409, **extra).json()
        assert refused['code'] == 'conflict' and 'still changing' in refused['detail']
    assert len(everything(api, project, database)) == 1


# ---- L2: rename and expiry ----

@pytest.mark.l2
def test_renaming_the_first_branch_renames_the_database(api, project, fresh):
    database, branch = fresh
    before = read(api, project, database, branch)
    name = name_for('renamed')
    after = update(api, project, database, branch, {'name': name.upper()}, etag=before.headers['ETag'])
    assert after.json()['name'] == name and after.json()['revision'] == before.json()['revision'] + 1
    assert after.headers['ETag'] == f'"{after.json()["revision"]}"'
    renamed = read_database(api, project, database).json()
    assert renamed['name'] == name and renamed['revision'] == after.json()['revision']
    same = update(api, project, database, branch, {'name': name})
    assert same.json() == after.json(), 'the same name changes nothing'
    stale = update(api, project, database, branch, {'name': name_for()}, etag=before.headers['ETag'], expect=412)
    assert stale.json()['code'] == 'revision_mismatch'


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'name': ''}, {'name': 'has space'}, {'unknown': 1}, {'is_default': True},
                                  {'expires_at': 'soon'}, {'expires_at': '2001-01-01T00:00:00Z'}])
def test_update_refuses_a_malformed_request(api, project, shared, body):
    response = update(api, project, shared[0], shared[1], body, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_update_refuses_a_taken_name(api, project, shared):
    refused = update(api, project, shared[0], shared[1], {'name': 'main'}, expect=409).json()
    assert refused['code'] == 'name_taken'


@pytest.mark.l2
def test_a_first_branch_cannot_expire(api, project, shared):
    database, branch, _ = shared
    before = read(api, project, database, branch).json()
    refused = update(api, project, database, branch, {'expires_at': later()}, expect=409).json()
    assert refused['code'] == 'conflict' and 'first branch' in refused['detail']
    kept = update(api, project, database, branch, {'expires_at': None}).json()
    assert kept == before, 'never expiring is what it already does'


# ---- L2: the default branch ----

@pytest.mark.l2
def test_the_default_moves_between_databases_and_one_branch_holds_it(api, project, fresh):
    database, branch = fresh
    main = next(d for d in databases(api, project) if d['name'] == 'main')
    made = api.call('setDefaultBranch', path=at(project, database, branch), expect=200)
    assert made.json()['is_default'] is True and made.headers['ETag'] == f'"{made.json()["revision"]}"'
    assert read(api, project, main['id'], first(main['id'])).json()['is_default'] is False
    held = {d['id']: d['holds_project_default'] for d in databases(api, project)}
    assert held[database] is True and held[main['id']] is False
    again = api.call('setDefaultBranch', path=at(project, database, branch), expect=200)
    assert again.json() == made.json(), 'making the default the default changes nothing'
    blocked = delete_database(api, project, database, read_database(api, project, database).json()['name'], '*',
                              expect=409).json()
    assert blocked['code'] == 'delete_blocked'
    api.call('setDefaultBranch', path=at(project, main['id'], first(main['id'])), expect=200)
    assert read(api, project, database, branch).json()['is_default'] is False


# ---- L2: suspend, resume and delete ----

@pytest.mark.l2
def test_suspending_a_first_branch_suspends_its_database(api, project, fresh):
    database, branch = fresh
    used = key()
    operation = change(api, 'suspendBranch', project, database, branch, used=used).json()
    assert operation['kind'] == 'database.suspend' and operation['target']['id'] == database
    assert operation['idempotency_key'] == used
    assert read(api, project, database, branch).json()['state'] == 'suspending'
    assert read_database(api, project, database).json()['state'] == 'suspending'
    assert change(api, 'suspendBranch', project, database, branch, used=used).json() == operation
    assert change(api, 'suspendBranch', project, database, branch).json() == operation, 'again changes nothing'
    refused = change(api, 'resumeBranch', project, database, branch, expect=409).json()
    assert refused['code'] == 'conflict' and 'in progress' in refused['detail']


@pytest.mark.l2
def test_resuming_a_branch_that_is_starting_changes_nothing(api, project, shared):
    database, branch, _ = shared
    before = read(api, project, database, branch).json()
    operation = change(api, 'resumeBranch', project, database, branch).json()
    assert operation['kind'] == 'database.create'
    assert read(api, project, database, branch).json() == before


@pytest.mark.l2
@pytest.mark.parametrize('operation', ['suspendBranch', 'resumeBranch'])
def test_a_state_change_requires_an_idempotency_key(api, project, shared, operation):
    response = api.call(operation, path=at(project, shared[0], shared[1]), expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_a_first_branch_is_deleted_with_its_database(api, project, shared):
    database, branch, _ = shared
    before = read(api, project, database, branch).json()
    assert delete(api, project, database, branch, etag=None, expect=428).json()['code'] == 'precondition_required'
    assert delete(api, project, database, branch, etag='"999"', expect=412).json()['code'] == 'revision_mismatch'
    refused = delete(api, project, database, branch, expect=409).json()
    assert refused['code'] == 'delete_blocked' and 'Delete the database' in refused['detail']
    missing = api.call('deleteBranch', path=at(project, database, branch), headers={'If-Match': '*'}, expect=400)
    assert missing.json()['code'] == 'invalid_request', 'a key is required'
    assert read(api, project, database, branch).json() == before


@pytest.mark.l2
def test_a_branch_of_a_database_being_deleted_accepts_no_changes(api, project, fresh):
    database, branch = fresh
    delete_database(api, project, database, read_database(api, project, database).json()['name'], '*')
    assert read(api, project, database, branch).json()['state'] == 'deleting'
    assert update(api, project, database, branch, {'name': name_for()}, expect=409).json()['code'] == 'conflict'
    assert api.call('setDefaultBranch', path=at(project, database, branch), expect=409).json()['code'] == 'conflict'
    for operation in ('suspendBranch', 'resumeBranch'):
        assert change(api, operation, project, database, branch, expect=409).json()['code'] == 'conflict'
    assert create(api, project, database, name_for(), expect=409).json()['code'] == 'conflict'


# ---- L5: durability ----

@pytest.mark.l5
def test_branch_facts_survive_a_killed_daemon(private_server):
    api = private_server.session()
    project = new_project(api)
    database = create_database(api, project, name_for()).json()['target']['id']
    branch = first(database)
    update(api, project, database, branch, {'name': name_for('kept')})
    api.call('setDefaultBranch', path=at(project, database, branch), expect=200)
    used = key()
    suspended = change(api, 'suspendBranch', project, database, branch, used=used).json()
    before = read(api, project, database, branch).json()
    private_server.restart(kill=True)
    api = private_server.session()
    assert read(api, project, database, branch).json() == before
    assert before['is_default'] is True and before['state'] == 'suspending'
    assert change(api, 'suspendBranch', project, database, branch, used=used).json() == suspended


# ---- L6: parity with the command line ----

@pytest.mark.l6
def test_the_api_and_the_command_line_see_the_same_branches(own, api, project, shared):
    database, branch, name = shared
    runtime = runtime_of(own, database)
    theirs = {b['branch']['id'].replace('-', ''): b for b in own.rpc(method='list_branches', project_id=runtime, include_deleted=False)}
    record = theirs[branch.removeprefix('br_')]
    found = read(api, project, database, branch).json()
    assert found['name'] == record['branch']['name'] == name
    assert found['is_default'] == record['is_default']
    assert found['parent_id'] is None and record['branch']['parent_id'] is None
    renamed = name_for('theirs')
    own.rpc(method='rename_branch', project_id=runtime, id=str(uuid.UUID(branch.removeprefix('br_'))), name=renamed)
    after = read(api, project, database, branch).json()
    assert after['name'] == renamed and after['revision'] == found['revision'] + 1
    update(api, project, database, branch, {'name': name})


# ---- With database engines: branches that exist ----

def rows(request, uri):
    return psql(request, uri, 'select string_agg(v, \',\' order by v) from t')


def address(api, project, database, branch):
    path = {'project': project, 'database': database}
    found = api.call('getDatabaseConnection', path=path, params={'branch': branch}, expect=200).json()
    assert found['branch_id'] == branch
    return api.call('revealDatabaseCredentials', path=path, json={'branch_id': branch}, expect=200).json()['uri']


@pytest.fixture(scope='module')
def live(engine_api, request):
    """A project on the engine server whose `main` database is running and
    holds one table, as (project, database, first branch, connection URI)."""
    made = engine_api.call('createProject', json={'name': name_for('branches')},
                           headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(engine_api, project, made)
    database = next(d for d in databases(engine_api, project) if d['name'] == 'main')['id']
    uri = address(engine_api, project, database, first(database))
    psql(request, uri, "create table t(v text); insert into t values ('a')")
    return project, database, first(database), uri


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_starts_from_its_parent_and_then_diverges(engine_api, live, request):
    api, (project, database, main, main_uri) = engine_api, live
    name, used = name_for('dev'), key()
    operation = create(api, project, database, name, used=used).json()
    assert operation['kind'] == 'branch.create' and operation['target']['kind'] == 'branch'
    branch = operation['target']['id']
    assert create(api, project, database, name, used=used).json()['id'] == operation['id'], 'a repeat is the same operation'
    done = finished(api, project, operation)
    assert done['result']['branch']['id'] == branch and done['idempotency_key'] == used
    found = read(api, project, database, branch).json()
    assert found['state'] == 'running' and found['parent_id'] == main and found['database_id'] == database
    assert found['created_from'] == {'kind': 'head'} and found['is_default'] is False
    assert read(api, project, database, main).json()['child_count'] == 1
    assert read_database(api, project, database).json()['branch_count'] == 2
    assert {b['id'] for b in everything(api, project, database)} == {main, branch}
    assert [b['id'] for b in everything(api, project, database, limit=1)] == [b['id'] for b in everything(api, project, database)]

    uri = address(api, project, database, branch)
    assert uri != main_uri, 'each branch has its own address'
    assert rows(request, uri) == 'a', 'it starts with what its parent had'
    psql(request, uri, "insert into t values ('branch-only')")
    psql(request, main_uri, "insert into t values ('b')")
    assert rows(request, uri) == 'a,branch-only' and rows(request, main_uri) == 'a,b'

    grandchild = finished(api, project, create(api, project, database, name_for('nested'), parent_id=branch).json())['target']['id']
    assert read(api, project, database, grandchild).json()['parent_id'] == branch
    assert rows(request, address(api, project, database, grandchild)) == 'a,branch-only'
    idle(api, project, database)
    blocked = delete(api, project, database, branch, expect=409).json()
    assert blocked['code'] == 'delete_blocked' and 'Delete them first' in blocked['detail']
    for gone in (grandchild, branch):
        wait(lambda: read(api, project, database, gone).json()['connections']['open'] == 0)
        etag = read(api, project, database, gone).headers['ETag']
        removal = delete(api, project, database, gone, etag=etag).json()
        assert removal['kind'] == 'branch.delete' and removal['target']['id'] == gone
        assert 'result' not in finished(api, project, removal)
        assert read(api, project, database, gone, expect=404).json()['code'] == 'not_found'
    assert [b['id'] for b in everything(api, project, database)] == [main]
    finished(api, project, create(api, project, database, name).json())  # the name is free again


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_starts_from_a_past_time_or_log_position(engine_api, live, request):
    api, (project, database, main, main_uri) = engine_api, live
    psql(request, main_uri, "insert into t values ('early')")
    position = psql(request, main_uri, 'select pg_current_wal_flush_lsn()')
    time.sleep(2)
    moment = later(0)
    time.sleep(2)
    psql(request, main_uri, "insert into t values ('late')")
    at_time = {'kind': 'time', 'timestamp': moment}
    at_position = {'kind': 'lsn', 'lsn': position}
    for point in (at_time, at_position):
        operation = create(api, project, database, name_for(point['kind']), **{'from': point}).json()
        branch = finished(api, project, operation)['target']['id']
        assert read(api, project, database, branch).json()['created_from'] == point
        seen = rows(request, address(api, project, database, branch))
        assert 'early' in seen and 'late' not in seen, point
    assert 'late' in rows(request, main_uri)


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_is_renamed_suspended_resumed_and_made_the_default(engine_api, live, request):
    api, (project, database, main, _) = engine_api, live
    expires = later(3600)
    operation = create(api, project, database, name_for('work'), expires_at=expires).json()
    branch = finished(api, project, operation)['target']['id']
    found = read(api, project, database, branch)
    assert found.json()['expires_at'] == expires
    uri = address(api, project, database, branch)

    assert api.call('setDefaultBranch', path=at(project, database, branch), expect=409).json()['code'] == 'conflict'
    name = name_for('renamed')
    changed = update(api, project, database, branch, {'name': name, 'expires_at': None}, etag=found.headers['ETag'])
    assert changed.json()['name'] == name and changed.json()['expires_at'] is None
    assert changed.json()['revision'] == found.json()['revision'] + 2, 'one for the name and one for the expiry'
    assert address(api, project, database, branch) == uri, 'a rename keeps the address'

    made = api.call('setDefaultBranch', path=at(project, database, branch), expect=200).json()
    assert made['is_default'] is True and read(api, project, database, main).json()['is_default'] is False
    assert read_database(api, project, database).json()['holds_project_default'] is True
    assert update(api, project, database, branch, {'expires_at': later()}, expect=409).json()['code'] == 'conflict'
    assert delete(api, project, database, branch, expect=409).json()['code'] == 'delete_blocked'
    api.call('setDefaultBranch', path=at(project, database, main), expect=200)

    wait(lambda: read(api, project, database, branch).json()['connections']['open'] == 0)
    suspended = finished(api, project, change(api, 'suspendBranch', project, database, branch).json())
    assert suspended['kind'] == 'branch.suspend' and suspended['result']['branch']['id'] == branch
    assert read(api, project, database, branch).json()['state'] == 'suspended'
    assert read(api, project, database, main).json()['state'] == 'running', 'its parent is not affected'
    assert change(api, 'suspendBranch', project, database, branch).json() == suspended
    resumed = finished(api, project, change(api, 'resumeBranch', project, database, branch).json())
    assert resumed['kind'] == 'branch.resume'
    assert read(api, project, database, branch).json()['state'] == 'running'
    assert change(api, 'resumeBranch', project, database, branch).json() == resumed
    kinds = [o['kind'] for o in api.call('listOperations', path={'project': project},
                                         params={'target': f'branch:{branch}'}).json()['items']]
    assert {'branch.create', 'branch.suspend', 'branch.resume', 'branch.set_expiry'} <= set(kinds)


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_deletes_itself_when_its_expiry_passes(engine_api, live):
    api, (project, database, _, _) = engine_api, live
    operation = create(api, project, database, name_for('brief')).json()
    branch = finished(api, project, operation)['target']['id']
    update(api, project, database, branch, {'expires_at': later(3)})
    wait(lambda: read(api, project, database, branch, expect=None).status_code == 404, timeout=120)
    assert branch not in [b['id'] for b in everything(api, project, database)]


# --- The same operations for signed-in callers on a governed server -----------

@pytest.mark.l2
@pytest.mark.parametrize('caller', ['viewer', 'editor', 'administrator'])
def test_a_signed_in_caller_reads_the_branches_of_their_project(caller, governed):
    path = dict(project=governed.project, database=governed.database)
    listed = governed.callers[caller].call('listBranches', path=path, expect=200).json()
    assert [b['id'] for b in listed['items']] == [governed.branch]
    found = governed.callers[caller].call('getBranch', path=dict(path, branch=governed.branch), expect=200).json()
    assert found['name'] == 'roles-db' and found['parent_id'] is None


@pytest.mark.l2
def test_a_branch_in_another_project_is_not_revealed(governed):
    path = dict(project=governed.other_project, database=governed.database, branch=governed.branch)
    assert governed.callers['elsewhere'].call('getBranch', path=path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_governed_server_does_not_change_branches_yet(governed):
    admin = governed.callers['administrator']
    two = dict(project=governed.project, database=governed.database)
    one = dict(two, branch=governed.branch)
    before = admin.call('getBranch', path=one, expect=200).json()
    attempts = [('createBranch', dict(path=two, json={'name': 'more'})),
                ('updateBranch', dict(path=one, json={'name': 'renamed'})),
                ('deleteBranch', dict(path=one, headers={'If-Match': '*'})),
                ('suspendBranch', dict(path=one)), ('resumeBranch', dict(path=one)),
                ('setDefaultBranch', dict(path=one))]
    for operation, arguments in attempts:
        arguments.setdefault('headers', {})['Idempotency-Key'] = key()
        refused = admin.call(operation, expect=403, **arguments).json()
        assert refused['code'] == 'unavailable_in_profile', operation
    assert admin.call('getBranch', path=one, expect=200).json() == before
