"""The backups domain: scenarios (L2), durability (L5) and parity (L6).

A backup is a point of a branch that storage keeps, so taking one needs the
database engines, and without them a database's first branch never finishes
starting. Here the engineless tests cover the schedule, every refusal and the
contract; the tests marked `engine` take real backups, restore them as
branches, check with SQL what each holds, and watch a schedule take and
remove its own.

These tests run on a server of their own, like the database tests.
"""
import re
import time

import pytest

from harness.server import wait
from test_branches import (address, at, create as create_branch, delete as delete_branch, everything as branches, first,
                           later, read as read_branch, rows)
from test_databases import (api, create as create_database, delete as delete_database, everything as databases,  # noqa: F401
                            finished, idle, key, name_for, new_project, own, project, psql, read as read_database,
                            runtime_of)

PUBLIC_ID = re.compile(r'^bkp_[0-9a-f]{32}$')
MISSING = 'bkp_' + '0' * 32
NONE = {'enabled': False, 'every_hours': None, 'keep': None, 'next_backup_at': None, 'updated_at': None}


def within(project, database):
    return {'project': project, 'database': database}


def everything(api, project, database, **params):
    """Every backup of a database, following cursors."""
    items, cursor = [], None
    while True:
        page = api.call('listBackups', path=within(project, database),
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


def take(api, project, database, expect=202, used=None, **body):
    return api.call('createBackup', path=within(project, database), json=body,
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def read(api, project, database, backup, expect=200):
    return api.call('getBackup', path=dict(within(project, database), backup=backup), expect=expect)


def delete(api, project, database, backup, expect=202, used=None):
    return api.call('deleteBackup', path=dict(within(project, database), backup=backup),
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def restore(api, project, database, backup, name, expect=202, used=None, **extra):
    return api.call('restoreBackup', path=dict(within(project, database), backup=backup), json={'name': name, **extra},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def schedule(api, project, database, expect=200):
    return api.call('getBackupSchedule', path=within(project, database), expect=expect)


def set_schedule(api, project, database, body, expect=200):
    return api.call('setBackupSchedule', path=within(project, database), json=body, expect=expect)


@pytest.fixture(scope='module')
def shared(api, project):
    """One database for tests that leave it without backups, as (database, first branch)."""
    database = create_database(api, project, name_for('kept')).json()['target']['id']
    return database, first(database)


# ---- L2: without engines ----

@pytest.mark.l2
def test_a_database_starts_with_no_backups_and_no_schedule(api, project, shared):
    database, _ = shared
    assert api.call('listBackups', path=within(project, database), expect=200).json() == {'items': [], 'next_cursor': None}
    assert schedule(api, project, database).json() == NONE


@pytest.mark.l2
def test_backups_belong_to_a_database_that_exists(api, project):
    nowhere = 'db_' + '0' * 32
    for response in [api.call('listBackups', path=within(project, nowhere), expect=404),
                     take(api, project, nowhere, expect=404), schedule(api, project, nowhere, expect=404),
                     set_schedule(api, project, nowhere, {'enabled': False}, expect=404)]:
        assert response.json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('backup', [MISSING, 'bkp_nothex', 'br_' + '0' * 32, 'x'])
def test_an_unknown_backup_is_not_found(api, project, shared, backup):
    database, _ = shared
    for response in [read(api, project, database, backup, expect=404), delete(api, project, database, backup, expect=404),
                     restore(api, project, database, backup, name_for(), expect=404)]:
        assert response.json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_branch_id_does_not_name_a_backup_and_a_first_branch_is_not_one(api, project, shared):
    database, branch = shared
    as_backup = 'bkp_' + branch.removeprefix('br_')
    assert read(api, project, database, as_backup, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_list_refuses_a_cursor_it_did_not_issue(api, project, shared):
    database, _ = shared
    for cursor in ['zz', '5b226e6f225d', '5b2261222c2262225d']:
        refused = api.call('listBackups', path=within(project, database), params={'cursor': cursor}, expect=400)
        assert refused.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('body', [{'name': ''}, {'name': 'x' * 65}, {'name': 'tab\there'}, {'name': 7},
                                  {'branch_id': 7}, {'unknown': 1}, []])
def test_taking_refuses_a_malformed_request(api, project, shared, body):
    database, _ = shared
    refused = api.call('createBackup', path=within(project, database), json=body,
                       headers={'Idempotency-Key': key()}, expect=400)
    assert refused.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_taking_requires_an_idempotency_key(api, project, shared):
    database, _ = shared
    refused = api.call('createBackup', path=within(project, database), json={}, expect=400).json()
    assert refused['code'] == 'invalid_request' and 'Idempotency-Key' in refused['detail']


@pytest.mark.l2
def test_taking_refuses_a_branch_that_is_not_in_the_database(api, project, shared):
    database, _ = shared
    main = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    for branch in [first(main), 'br_' + '0' * 32, 'nonsense']:
        assert take(api, project, database, expect=404, branch_id=branch).json()['code'] == 'not_found'


@pytest.mark.l2
def test_taking_waits_for_a_branch_that_is_still_starting(api, project, shared):
    """Without engines a first branch never finishes starting, and a backup of
    a branch that does not exist yet would hold nothing."""
    database, _ = shared
    refused = take(api, project, database, expect=409).json()
    assert refused['code'] == 'conflict' and 'still in progress' in refused['detail']
    assert everything(api, project, database) == []


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'name': ''}, {'name': 'has space'}, {'name': 'ok', 'unknown': 1},
                                  {'name': 'ok', 'expires_at': 'soon'}])
def test_restoring_checks_the_backup_before_the_request(api, project, shared, body):
    """An unknown backup is not found whatever is sent with it."""
    database, _ = shared
    refused = api.call('restoreBackup', path=dict(within(project, database), backup=MISSING), json=body,
                       headers={'Idempotency-Key': key()}, expect=404)
    assert refused.json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_schedule_is_set_read_changed_and_removed(api, project):
    database = create_database(api, project, name_for('sched')).json()['target']['id']
    before = time.time()
    made = set_schedule(api, project, database, {'enabled': True, 'every_hours': 24, 'keep': 7}).json()
    assert made['enabled'] is True and made['every_hours'] == 24 and made['keep'] == 7
    assert made['next_backup_at'] == made['updated_at'], 'the first backup is due at once'
    assert schedule(api, project, database).json() == made
    assert set_schedule(api, project, database, {'enabled': True, 'every_hours': 24, 'keep': 7}).json() == made, \
        'the same schedule again changes nothing'
    time.sleep(0.01)
    changed = set_schedule(api, project, database, {'enabled': True, 'every_hours': 6, 'keep': 30}).json()
    assert changed['every_hours'] == 6 and changed['keep'] == 30 and changed['updated_at'] > made['updated_at']
    assert set_schedule(api, project, database, {'enabled': False}).json() == NONE
    assert schedule(api, project, database).json() == NONE
    assert set_schedule(api, project, database, {'enabled': False}).json() == NONE
    assert time.time() - before < 30
    # Without engines nothing is taken, however due the schedule is.
    set_schedule(api, project, database, {'enabled': True, 'every_hours': 1, 'keep': 1})
    time.sleep(1.5)
    assert everything(api, project, database) == []
    set_schedule(api, project, database, {'enabled': False})
    delete_database(api, project, database, read_database(api, project, database).json()['name'], '*')


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'enabled': True}, {'enabled': True, 'every_hours': 24}, {'enabled': True, 'keep': 7},
    {'enabled': True, 'every_hours': 0, 'keep': 7}, {'enabled': True, 'every_hours': 169, 'keep': 7},
    {'enabled': True, 'every_hours': 24, 'keep': 0}, {'enabled': True, 'every_hours': 24, 'keep': 31},
    {'enabled': True, 'every_hours': '24', 'keep': 7}, {'enabled': True, 'every_hours': 1.5, 'keep': 7},
    {'enabled': False, 'every_hours': 24, 'keep': 7}, {'enabled': 'yes', 'every_hours': 24, 'keep': 7},
    {'enabled': True, 'every_hours': 24, 'keep': 7, 'unknown': 1}, []])
def test_a_schedule_refuses_a_malformed_request(api, project, shared, body):
    database, _ = shared
    assert set_schedule(api, project, database, body, expect=400).json()['code'] == 'invalid_request'
    assert schedule(api, project, database).json() == NONE


# ---- L5: durability ----

@pytest.mark.l5
def test_a_schedule_survives_a_killed_daemon(private_server):
    api = private_server.session()
    project = new_project(api)
    database = create_database(api, project, name_for()).json()['target']['id']
    made = set_schedule(api, project, database, {'enabled': True, 'every_hours': 12, 'keep': 3}).json()
    private_server.restart(kill=True)
    api = private_server.session()
    assert schedule(api, project, database).json() == made
    assert everything(api, project, database) == []


# ---- With database engines: backups that exist ----

@pytest.fixture(scope='module')
def live(engine_api, request):
    """A project on the engine server with a running database of its own that
    holds one table, as (project, database, first branch, connection URI)."""
    made = engine_api.call('createProject', json={'name': name_for('backups')},
                           headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(engine_api, project, made)
    operation = create_database(engine_api, project, name_for('orders')).json()
    database = finished(engine_api, project, operation)['target']['id']
    uri = address(engine_api, project, database, first(database))
    psql(request, uri, "create table t(v text); insert into t values ('a')")
    return project, database, first(database), uri


@pytest.mark.engine
@pytest.mark.l2
def test_a_backup_keeps_a_moment_and_restores_as_a_branch(engine_api, live, request):
    api, (project, database, main, uri) = engine_api, live
    used = key()
    operation = take(api, project, database, used=used, name='Before the change').json()
    assert operation['kind'] == 'backup.create' and operation['target']['kind'] == 'backup'
    assert operation['target']['label'] == 'Before the change'
    backup = operation['target']['id']
    assert PUBLIC_ID.match(backup)
    assert take(api, project, database, used=used, name='Before the change').json()['id'] == operation['id'], \
        'a repeat is the same operation'
    assert take(api, project, database, used=used, name='Another', expect=409).json()['code'] == 'idempotency_key_reused'
    done = finished(api, project, operation)
    assert done['result']['backup'] == {'kind': 'backup', 'id': backup} and done['idempotency_key'] == used

    found = read(api, project, database, backup).json()
    assert found['state'] == 'ready' and found['origin'] == 'manual' and found['name'] == 'Before the change'
    assert found['database_id'] == database and found['branch_id'] == main and found['error'] is None
    assert found['branch_name'] == read_branch(api, project, database, main).json()['name']
    assert re.match(r'^[0-9A-F]+/[0-9A-F]+$', found['lsn']) and found['restored_branch_count'] == 0
    assert everything(api, project, database) == [found]

    # The record that holds it is not a branch: nothing lists it or reaches it.
    as_branch = 'br_' + backup.removeprefix('bkp_')
    assert [b['id'] for b in branches(api, project, database)] == [main]
    assert read_branch(api, project, database, as_branch, expect=404).json()['code'] == 'not_found'
    assert read_database(api, project, database).json()['branch_count'] == 1
    assert read_branch(api, project, database, main).json()['child_count'] == 0
    for operation_id in ['suspendBranch', 'resumeBranch']:
        api.call(operation_id, path=at(project, database, as_branch), headers={'Idempotency-Key': key()}, expect=404)
    api.call('getDatabaseConnection', path=within(project, database), params={'branch': as_branch}, expect=404)

    # The branch moves on; the backup does not.
    psql(request, uri, "insert into t values ('later'); create table u(n int)")
    name, again = name_for('restored'), key()
    restoring = restore(api, project, database, backup, name, used=again).json()
    assert restoring['kind'] == 'branch.create' and restoring['target']['kind'] == 'branch'
    assert restore(api, project, database, backup, name, used=again).json()['id'] == restoring['id']
    assert restore(api, project, database, backup, name, expect=409).json()['code'] == 'name_taken'
    branch = finished(api, project, restoring)['target']['id']
    restored = read_branch(api, project, database, branch).json()
    assert restored['state'] == 'running' and restored['name'] == name
    assert restored['created_from'] == {'kind': 'backup', 'backup_id': backup}
    assert restored['parent_id'] == main, 'it stands under the branch the backup was taken from'
    theirs = address(api, project, database, branch)
    assert rows(request, theirs) == 'a' and rows(request, uri) == 'a,later'
    assert psql(request, theirs, "select to_regclass('u') is null") == 't'
    psql(request, theirs, "insert into t values ('restored-only')")
    assert rows(request, uri) == 'a,later', 'the two are separate from then on'
    assert read(api, project, database, backup).json()['restored_branch_count'] == 1
    assert read_branch(api, project, database, main).json()['child_count'] == 1
    assert {b['id'] for b in branches(api, project, database)} == {main, branch}

    # A second restore of the same backup holds what the first did at the start.
    second = finished(api, project, restore(api, project, database, backup, name_for('twice'), expires_at=later()).json())
    twice = second['target']['id']
    assert rows(request, address(api, project, database, twice)) == 'a'
    assert read_branch(api, project, database, twice).json()['expires_at'] is not None

    # What stands on what, and the order things can go in.
    idle(api, project, database)
    blocked = delete(api, project, database, backup, expect=409).json()
    assert blocked['code'] == 'delete_blocked' and 'restored from it' in blocked['detail']
    wait(lambda: read_branch(api, project, database, main).json()['connections']['open'] == 0)
    for gone in (branch, twice):
        wait(lambda: read_branch(api, project, database, gone).json()['connections']['open'] == 0)
        finished(api, project, delete_branch(api, project, database, gone).json())
    removal = delete(api, project, database, backup).json()
    assert removal['kind'] == 'backup.delete' and removal['target'] == {'kind': 'backup', 'id': backup,
                                                                         'label': 'Before the change'}
    assert 'result' not in finished(api, project, removal)
    assert read(api, project, database, backup, expect=404).json()['code'] == 'not_found'
    assert everything(api, project, database) == []
    assert rows(request, uri) == 'a,later', 'deleting a backup leaves its branch alone'


@pytest.mark.engine
@pytest.mark.l2
def test_backups_of_a_database_are_listed_newest_first_and_block_its_removal(engine_api, live, request):
    api, (project, _, _, _) = engine_api, live
    database = finished(api, project, create_database(api, project, name_for('listed')).json())['target']['id']
    main = first(database)
    uri = address(api, project, database, main)
    psql(request, uri, "create table t(v text); insert into t values ('one')")
    child = finished(api, project, create_branch(api, project, database, name_for('side')).json())['target']['id']
    taken = []
    for name, source in [('first', None), ('second', child), (None, main)]:
        body = {**({'name': name} if name else {}), **({'branch_id': source} if source else {})}
        taken.append(finished(api, project, take(api, project, database, **body).json())['target']['id'])
        time.sleep(0.05)
    listed = everything(api, project, database)
    assert [b['id'] for b in listed] == taken[::-1]
    assert [b['branch_id'] for b in listed] == [main, child, main]
    assert re.match(r'^\d{4}-\d\d-\d\d \d\d:\d\d UTC$', listed[0]['name']), 'the default name is when it was taken'
    assert [b['id'] for b in everything(api, project, database, limit=1)] == taken[::-1]
    assert [b['id'] for b in everything(api, project, database, limit=2)] == taken[::-1]
    other = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    assert everything(api, project, other) == [], 'another database of the project sees none of them'
    assert read(api, project, other, taken[0], expect=404).json()['code'] == 'not_found'

    idle(api, project, database)
    wait(lambda: read_branch(api, project, database, child).json()['connections']['open'] == 0)
    blocked = delete_branch(api, project, database, child, expect=409).json()
    assert blocked['code'] == 'delete_blocked' and 'backups' in blocked['detail']
    finished(api, project, delete(api, project, database, taken[1]).json())
    finished(api, project, delete_branch(api, project, database, child).json())
    name = read_database(api, project, database).json()['name']
    blocked = delete_database(api, project, database, name, '*', expect=409).json()
    assert blocked['code'] == 'delete_blocked' and 'backups' in blocked['detail']
    for backup in (taken[0], taken[2]):
        finished(api, project, delete(api, project, database, backup).json())
    finished(api, project, delete_database(api, project, database, name, '*').json())


@pytest.mark.engine
@pytest.mark.l2
def test_a_backup_of_a_suspended_branch_leaves_it_suspended(engine_api, live, request):
    api, (project, _, _, _) = engine_api, live
    database = finished(api, project, create_database(api, project, name_for('asleep')).json())['target']['id']
    main = first(database)
    psql(request, address(api, project, database, main), "create table t(v text); insert into t values ('kept')")
    idle(api, project, database)
    finished(api, project, api.call('suspendDatabase', path=within(project, database),
                                    headers={'Idempotency-Key': key()}, expect=202).json())
    backup = finished(api, project, take(api, project, database).json())['target']['id']
    assert read(api, project, database, backup).json()['state'] == 'ready'
    wait(lambda: read_database(api, project, database).json()['state'] == 'suspended', timeout=120)
    branch = finished(api, project, restore(api, project, database, backup, name_for('woken')).json())['target']['id']
    assert rows(request, address(api, project, database, branch)) == 'kept'
    assert read_database(api, project, database).json()['state'] == 'suspended', 'restoring wakes nothing else'
    idle(api, project, database)
    wait(lambda: read_branch(api, project, database, branch).json()['connections']['open'] == 0)
    finished(api, project, delete_branch(api, project, database, branch).json())
    finished(api, project, delete(api, project, database, backup).json())
    finished(api, project, delete_database(api, project, database, read_database(api, project, database).json()['name'], '*').json())


@pytest.mark.engine
@pytest.mark.l2
def test_a_schedule_takes_backups_keeps_the_newest_and_spares_the_rest(engine_api, live, request):
    """The engine server's schedule hour is three seconds long."""
    api, (project, _, _, _) = engine_api, live
    database = finished(api, project, create_database(api, project, name_for('nightly')).json())['target']['id']
    main = first(database)
    uri = address(api, project, database, main)
    psql(request, uri, "create table t(v text); insert into t values ('a')")
    manual = finished(api, project, take(api, project, database, name='By hand').json())['target']['id']
    set_schedule(api, project, database, {'enabled': True, 'every_hours': 1, 'keep': 2})

    def scheduled(state='ready'):
        return [b for b in everything(api, project, database) if b['origin'] == 'scheduled' and b['state'] == state]
    earliest = wait(lambda: scheduled(), timeout=60)[-1]
    assert earliest['branch_id'] == main and earliest['name'].startswith('Scheduled ')
    # One that a branch was restored from outlives the count.
    psql(request, uri, "insert into t values ('b')")
    branch = finished(api, project, restore(api, project, database, earliest['id'], name_for('pinned')).json())['target']['id']
    wait(lambda: len(scheduled()) >= 3 and earliest['id'] not in [b['id'] for b in scheduled()[:2]], timeout=90)
    # The newest two and the one a branch stands on; a fourth appears only for
    # the moment between a new one being ready and the oldest spare being removed.
    seen = []
    for _ in range(40):
        kept = scheduled()
        assert earliest['id'] in [b['id'] for b in kept], 'a branch stands on it'
        seen.append(len(kept))
        time.sleep(0.25)
    assert 3 in seen and max(seen) <= 4, seen
    assert manual in [b['id'] for b in everything(api, project, database)], 'the schedule deletes only its own'
    assert schedule(api, project, database).json()['next_backup_at'] is not None
    assert rows(request, address(api, project, database, branch)) == 'a'

    # A suspended database is not woken, and nothing new is taken.
    idle(api, project, database)
    wait(lambda: read_branch(api, project, database, branch).json()['connections']['open'] == 0)
    finished(api, project, delete_branch(api, project, database, branch).json())
    wait(lambda: len(scheduled()) == 2, timeout=60)
    # A backup being taken at that moment holds the database awake until it is.
    def suspended():
        asked = api.call('suspendDatabase', path=within(project, database), headers={'Idempotency-Key': key()})
        assert asked.status_code in (202, 409), asked.text
        return asked.json() if asked.status_code == 202 else None
    finished(api, project, wait(suspended, timeout=60))
    wait(lambda: not scheduled('creating') and not scheduled('deleting'), timeout=60)
    before = [b['id'] for b in scheduled()]
    time.sleep(8)
    assert [b['id'] for b in scheduled()] == before
    assert read_database(api, project, database).json()['state'] == 'suspended'

    # Removing the schedule keeps what it took.
    assert set_schedule(api, project, database, {'enabled': False}).json() == NONE
    assert [b['id'] for b in scheduled()] == before
    for backup in everything(api, project, database):
        finished(api, project, delete(api, project, database, backup['id']).json())
    finished(api, project, delete_database(api, project, database, read_database(api, project, database).json()['name'], '*').json())


@pytest.mark.engine
@pytest.mark.l6
def test_the_command_line_sees_a_backup_as_a_record_it_cannot_run(engine_api, engine_server, live):
    """The command line is unchanged, so it lists the record that holds a
    backup. It must not be able to start it or change it."""
    api, (project, database, main, _) = engine_api, live
    backup = finished(api, project, take(api, project, database, name='Seen from outside').json())['target']['id']
    runtime = runtime_of(engine_server, database)
    theirs = {b['branch']['id'].replace('-', ''): b
              for b in engine_server.rpc(method='list_branches', project_id=runtime, include_deleted=False)}
    record = theirs[backup.removeprefix('bkp_')]
    assert record['branch']['name'].startswith('backup-') and record['ports'] is None
    assert record['branch']['parent_id'].replace('-', '') == main.removeprefix('br_')
    found = read(api, project, database, backup).json()
    assert found['lsn'] == record['branch']['ancestor_lsn']
    finished(api, project, delete(api, project, database, backup).json())


# ---- A governed server ----

@pytest.mark.l2
@pytest.mark.parametrize('caller', ['viewer', 'editor', 'administrator'])
def test_a_signed_in_caller_reads_the_backups_of_their_project(caller, governed):
    path = dict(project=governed.project, database=governed.database)
    assert governed.callers[caller].call('listBackups', path=path, expect=200).json() == {'items': [], 'next_cursor': None}
    assert governed.callers[caller].call('getBackupSchedule', path=path, expect=200).json() == NONE
    assert governed.callers[caller].call('getBackup', path=dict(path, backup=MISSING), expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_backups_in_another_project_are_not_revealed(governed):
    path = dict(project=governed.other_project, database=governed.database)
    assert governed.callers['elsewhere'].call('listBackups', path=path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_governed_server_does_not_change_backups_yet(governed):
    admin = governed.callers['administrator']
    two = dict(project=governed.project, database=governed.database)
    one = dict(two, backup=MISSING)
    attempts = [('createBackup', dict(path=two, json={})), ('deleteBackup', dict(path=one)),
                ('restoreBackup', dict(path=one, json={'name': 'more'})),
                ('setBackupSchedule', dict(path=two, json={'enabled': False}))]
    for operation, arguments in attempts:
        arguments.setdefault('headers', {})['Idempotency-Key'] = key()
        refused = admin.call(operation, expect=403, **arguments).json()
        assert refused['code'] == 'unavailable_in_profile', operation
    assert admin.call('getBackupSchedule', path=two, expect=200).json() == NONE
