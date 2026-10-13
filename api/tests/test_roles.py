"""The roles domain: scenarios (L2).

Roles live in a branch's PostgreSQL, so without the database engines the tests
here cover only what is refused before PostgreSQL is asked. The tests marked
`engine` create real roles and then connect as them, because what a role may
do is only proved by PostgreSQL letting it, or not.

These tests run on a server of their own, like the database tests.
"""
import pathlib
import re
import subprocess
import uuid

import pytest

from harness.server import wait
from test_branches import at, create as create_branch, first
from test_databases import api, everything as databases, finished, key, name_for, new_project, own, project  # noqa: F401
from test_sql import live, main_of, values, write  # noqa: F401

NO_BRANCH = 'br_' + '0' * 32
ENGINE_ROLES = {'cloud_admin', 'supabricks_owner', 'pg_read_all_data', 'pg_write_all_data', 'pg_monitor'}
PLAIN = dict(managed=False, login=True, superuser=False, create_role=False, create_database=False,
             replication=False, bypass_rls=False, connection_limit=None)


def role_name(prefix='r'):
    return f'{prefix}_{uuid.uuid4().hex[:10]}'


def roles(api, place, **params):
    found, cursor = [], None
    while True:
        page = api.call('listRoles', path=place, params={**params, **({'cursor': cursor} if cursor else {})},
                        expect=200).json()
        found += page['items']
        cursor = page['next_cursor']
        if not cursor:
            return found


def make(api, place, name=None, expect=201, **body):
    return api.call('createRole', path=place, json={'name': name or role_name(), **body}, expect=expect)


def role(api, place, name, expect=200):
    return api.call('getRole', path={**place, 'role': name}, expect=expect)


def change(api, place, name, expect=200, **body):
    return api.call('updateRole', path={**place, 'role': name}, json=body, expect=expect)


def remove(api, place, name, expect=204):
    return api.call('deleteRole', path={**place, 'role': name}, expect=expect)


def new_password(api, place, name, expect=200):
    return api.call('resetRolePassword', path={**place, 'role': name}, expect=expect)


def grant(api, place, name, schema='public', expect=200, **body):
    body = {'usage': True, 'create': False, 'tables': [], **body}
    return api.call('setRolePrivileges', path={**place, 'role': name, 'schema': schema}, json=body, expect=expect)


def ask(request, uri, sql):
    """Run SQL as whoever the URI names: (worked, output or PostgreSQL's complaint)."""
    binary = pathlib.Path(request.config.getoption('--bundle')) / 'pg_install/v17/bin/psql'
    result = subprocess.run([str(binary), uri, '-qAt', '-v', 'ON_ERROR_STOP=1', '-c', sql],
                            capture_output=True, text=True, timeout=60)
    return result.returncode == 0, (result.stdout if result.returncode == 0 else result.stderr).strip()


def denied(request, uri, sql):
    worked, said = ask(request, uri, sql)
    return not worked and 'permission denied' in said


# ---- L2: what is refused before PostgreSQL is asked ----

@pytest.fixture(scope='module')
def place(api, project):
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    return at(project, database, first(database))


@pytest.mark.l2
def test_roles_need_the_database_engines(api, place):
    for response in [api.call('listRoles', path=place, expect=503), role(api, place, 'reporting', expect=503),
                     make(api, place, expect=503), change(api, place, 'reporting', login=False, expect=503),
                     remove(api, place, 'reporting', expect=503), new_password(api, place, 'reporting', expect=503),
                     grant(api, place, 'reporting', expect=503)]:
        assert response.json()['code'] == 'unavailable'


@pytest.mark.l2
def test_roles_belong_to_a_branch_that_exists(api, place):
    nowhere = {**place, 'branch': NO_BRANCH}
    for response in [api.call('listRoles', path=nowhere, expect=404), role(api, nowhere, 'reporting', expect=404),
                     make(api, nowhere, expect=404), change(api, nowhere, 'reporting', login=False, expect=404),
                     remove(api, nowhere, 'reporting', expect=404),
                     new_password(api, nowhere, 'reporting', expect=404), grant(api, nowhere, 'reporting', expect=404)]:
        assert response.json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'name': ''}, {'name': 'Reporting'}, {'name': '1st'}, {'name': 'has space'}, {'name': 'a-b'},
    {'name': 'a' * 64}, {'name': 'pg_mine'}, {'name': 'sb_mine'}, {'name': 'supabricks_mine'},
    {'name': 'neon_mine'}, {'name': 'cloud_mine'}, {'name': 'a"; drop role cloud_admin; --'}, {'name': 7},
    {'name': 'ok', 'access': 'admin'}, {'name': 'ok', 'access': 'custom'}, {'name': 'ok', 'access': 7},
    {'name': 'ok', 'password': 'chosen'}, {'name': 'ok', 'superuser': True}, {'name': 'ok', 'login': False},
])
def test_creating_a_role_rejects_an_invalid_body(api, place, body):
    response = api.raw('POST', api_path(place), json=body)
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'invalid_request'


def api_path(place, *more):
    return '/'.join([f"/projects/{place['project']}/databases/{place['database']}/branches/{place['branch']}/roles",
                     *more])


@pytest.mark.l2
@pytest.mark.parametrize('name', ['cloud_admin', 'supabricks_owner', 'pg_read_all_data', 'neon_superuser', 'sb_x'])
def test_a_role_the_engine_manages_cannot_be_changed(api, place, name):
    for response in [change(api, place, name, login=False, expect=409), remove(api, place, name, expect=409),
                     new_password(api, place, name, expect=409), grant(api, place, name, expect=409)]:
        assert response.json()['code'] == 'conflict'


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'login': 'no'}, {'access': 'custom'}, {'access': 'admin'},
                                  {'name': 'renamed'}, {'superuser': True}, {'connection_limit': 3}])
def test_changing_a_role_rejects_an_invalid_body(api, place, body):
    response = api.raw('PATCH', api_path(place, 'reporting'), json=body)
    assert response.status_code == 400, response.text


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'usage': True, 'create': False}, {'usage': 'yes', 'create': False, 'tables': []},
    {'usage': True, 'create': False, 'tables': 'select'}, {'usage': True, 'create': False, 'tables': ['SELECT']},
    {'usage': True, 'create': False, 'tables': ['select', 'select']},
    {'usage': True, 'create': False, 'tables': ['select', 'drop']},
    {'usage': True, 'create': False, 'tables': [], 'columns': ['id']},
])
def test_setting_privileges_rejects_an_invalid_body(api, place, body):
    response = api.raw('PUT', api_path(place, 'reporting', 'privileges', 'public'), json=body)
    assert response.status_code == 400, response.text


@pytest.mark.l2
@pytest.mark.parametrize('schema', ['pg_catalog', 'information_schema', 'pg_toast', '_supabricks', 'neon'])
def test_privileges_are_not_set_in_a_schema_that_is_not_the_projects(api, place, schema):
    assert grant(api, place, 'reporting', schema=schema, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_role_password_reset_takes_no_body(api, place):
    response = api.raw('POST', api_path(place, 'reporting', 'password'), json={'password': 'chosen'})
    assert response.status_code == 400, response.text


# ---- With database engines ----

@pytest.fixture(scope='module')
def lived(engine_api, live):
    """The branch of `live`, with one table the application role owns."""
    project, database, branch = live
    write(engine_api, project, branch, 'create table public.ledger as select 1 as id, 10 as amount')
    return at(project, database, branch)


@pytest.mark.engine
@pytest.mark.l2
def test_the_engines_own_roles_are_listed_as_managed(engine_api, lived):
    api = engine_api
    found = {r['name']: r for r in roles(api, lived)}
    assert ENGINE_ROLES <= set(found)
    assert all(found[name]['managed'] and found[name]['access'] is None for name in ENGINE_ROLES)
    assert found['cloud_admin']['superuser'] is True and found['supabricks_owner']['login'] is True
    assert found['pg_monitor']['member_of'] == ['pg_read_all_settings', 'pg_read_all_stats', 'pg_stat_scan_tables']
    assert found['pg_read_all_data']['login'] is False
    names = [r['name'] for r in roles(api, lived)]
    assert names == sorted(names)
    assert [r['name'] for r in roles(api, lived, limit=1)] == names
    assert [r['name'] for r in roles(api, lived, limit=7)] == names
    described = role(api, lived, 'supabricks_owner').json()
    assert described['managed'] is True and [p['schema'] for p in described['privileges']] == ['public']
    assert role(api, lived, 'nobody_here', expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_a_role_is_created_with_a_password_shown_once_and_an_access_level(engine_api, lived, request):
    api, name = engine_api, role_name('reader')
    made = make(api, lived, name, access='read')
    created = made.json()
    assert made.headers['Cache-Control'] == 'no-store'
    assert made.headers['Location'].endswith(f"/branches/{lived['branch']}/roles/{name}")
    assert re.fullmatch(r'[0-9a-f-]{72}', created['password']) and created['password'] in created['uri']
    assert created['uri'].startswith(f'postgresql://{name}:')
    expected = dict(PLAIN, name=name, access='read', member_of=['pg_read_all_data'], connections=0)
    assert {k: created[k] for k in expected} == expected

    described = role(api, lived, name).json()
    assert 'password' not in described and 'uri' not in described
    assert {k: described[k] for k in expected} == expected
    assert described['privileges'] == [dict(schema='public', usage=False, create=False, tables=[],
                                            future_tables=[], uniform=True)]
    assert next(r for r in roles(api, lived) if r['name'] == name) == {k: described[k] for k in described
                                                                       if k != 'privileges'}

    # PostgreSQL agrees: the role reads, and does nothing else.
    assert ask(request, created['uri'], 'select current_user, amount from public.ledger') == (True, f'{name}|10')
    assert denied(request, created['uri'], 'insert into public.ledger values (2, 20)')
    assert denied(request, created['uri'], 'create table public.mine (id int)')
    assert denied(request, created['uri'], 'create role another')

    again = make(api, lived, name, access='read', expect=409).json()
    assert again['code'] == 'name_taken'
    assert ask(request, created['uri'], 'select 1') == (True, '1'), 'a refused repeat changes nothing'


@pytest.mark.engine
@pytest.mark.l2
def test_an_access_level_and_login_can_be_changed(engine_api, lived, request):
    api, name = engine_api, role_name('writer')
    created = make(api, lived, name).json()
    uri = created['uri']
    assert created['access'] == 'none' and created['member_of'] == []
    assert denied(request, uri, 'select amount from public.ledger')

    changed = change(api, lived, name, access='read_write').json()
    assert (changed['access'], changed['member_of']) == ('read_write', ['pg_read_all_data', 'pg_write_all_data'])
    assert ask(request, uri, 'insert into public.ledger values (7, 70) returning id')[0]
    worked, said = ask(request, uri, 'drop table public.ledger')
    assert not worked and 'must be owner' in said

    changed = change(api, lived, name, access='owner').json()
    assert (changed['access'], changed['member_of']) == ('owner', ['supabricks_owner'])
    assert ask(request, uri, 'alter table public.ledger add column note text')[0]
    assert ask(request, uri, 'alter table public.ledger drop column note')[0]

    changed = change(api, lived, name, access='none', login=False).json()
    assert (changed['access'], changed['member_of'], changed['login']) == ('none', [], False)
    worked, said = ask(request, uri, 'select 1')
    assert not worked and 'not permitted to log in' in said
    assert change(api, lived, name, login=True).json()['login'] is True
    assert ask(request, uri, 'select 1') == (True, '1')
    assert denied(request, uri, 'select amount from public.ledger')
    assert change(api, lived, 'nobody_here', login=False, expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_a_role_gets_a_new_password_and_the_application_role_does_not_here(engine_api, lived, request):
    api, name = engine_api, role_name('rotate')
    old = make(api, lived, name).json()
    reset = new_password(api, lived, name)
    new = reset.json()
    assert reset.headers['Cache-Control'] == 'no-store'
    assert (new['role'], new['expires_at']) == (name, None) and new['password'] != old['password']
    assert ask(request, new['uri'], 'select current_user') == (True, name)
    worked, said = ask(request, old['uri'], 'select 1')
    assert not worked and 'password authentication failed' in said
    assert new_password(api, lived, 'nobody_here', expect=404).json()['code'] == 'not_found'
    refused = new_password(api, lived, 'supabricks_owner', expect=409).json()
    assert 'credentials' in refused['detail']


@pytest.mark.engine
@pytest.mark.l2
def test_privileges_are_set_schema_by_schema_and_cover_tables_made_later(engine_api, lived, request):
    api, name = engine_api, role_name('clerk')
    project, branch = lived['project'], lived['branch']
    uri = make(api, lived, name).json()['uri']
    schema = 's_' + uuid.uuid4().hex[:8]
    write(api, project, branch, f'create schema "{schema}"')
    write(api, project, branch, f'create table "{schema}".orders as select 1 as id')
    assert denied(request, uri, f'select id from "{schema}".orders')

    granted = grant(api, lived, name, schema=schema, tables=['select', 'insert']).json()
    assert granted == dict(schema=schema, usage=True, create=False, tables=['select', 'insert'],
                           future_tables=['select', 'insert'], uniform=True)
    assert ask(request, uri, f'insert into "{schema}".orders values (2) returning id') == (True, '2')
    assert denied(request, uri, f'delete from "{schema}".orders')
    assert denied(request, uri, f'create table "{schema}".mine (id int)')
    assert denied(request, uri, 'select amount from public.ledger'), 'another schema is untouched'

    # A table the application role makes afterwards carries the same grants.
    write(api, project, branch, f'create table "{schema}".later as select 5 as id')
    assert ask(request, uri, f'select id from "{schema}".later') == (True, '5')
    listed = {p['schema']: p for p in role(api, lived, name).json()['privileges']}
    assert listed[schema] == granted and listed['public']['tables'] == []

    # A grant changed table by table with a statement shows as not uniform.
    write(api, project, branch, f'revoke insert on "{schema}".later from "{name}"')
    uneven = {p['schema']: p for p in role(api, lived, name).json()['privileges']}[schema]
    assert (uneven['tables'], uneven['uniform']) == (['select'], False)

    replaced = grant(api, lived, name, schema=schema, create=True, tables=['delete']).json()
    assert replaced == dict(schema=schema, usage=True, create=True, tables=['delete'],
                            future_tables=['delete'], uniform=True)
    assert denied(request, uri, f'select id from "{schema}".orders')
    assert ask(request, uri, f'create table "{schema}".mine (id int)')[0]

    none = grant(api, lived, name, schema=schema, usage=False).json()
    assert (none['usage'], none['create'], none['tables'], none['future_tables']) == (False, False, [], [])
    assert denied(request, uri, f'delete from "{schema}".orders')
    assert grant(api, lived, name, schema='no_such_schema', expect=404).json()['code'] == 'not_found'
    assert grant(api, lived, 'nobody_here', schema=schema, expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_deleting_a_role_closes_its_connections_and_keeps_what_it_made(engine_api, lived, request):
    api, name = engine_api, role_name('maker')
    project, branch = lived['project'], lived['branch']
    uri = make(api, lived, name, access='owner').json()['uri']
    table = 't_' + uuid.uuid4().hex[:8]
    assert ask(request, uri, f'create table public."{table}" as select 3 as id')[0]
    grant(api, lived, name, tables=['select'])
    binary = pathlib.Path(request.config.getoption('--bundle')) / 'pg_install/v17/bin/psql'
    working = subprocess.Popen([str(binary), uri, '-qAt', '-c', 'select pg_sleep(60)'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait(lambda: role(api, lived, name).json()['connections'] == 1)

    remove(api, lived, name)
    assert working.wait(timeout=30) != 0, 'its connection was closed'
    assert role(api, lived, name, expect=404).json()['code'] == 'not_found'
    assert name not in [r['name'] for r in roles(api, lived)]
    assert remove(api, lived, name, expect=404).json()['code'] == 'not_found'
    worked, said = ask(request, uri, 'select 1')
    assert not worked and 'password authentication failed' in said
    assert values(api, project, branch, f"select tableowner from pg_tables where tablename = '{table}'") == [
        ['supabricks_owner']]
    assert values(api, project, branch, f'select id from public."{table}"') == [['3']]

    # The name is free again, and the new role starts with nothing.
    again = make(api, lived, name).json()
    assert again['password'] not in uri and denied(request, again['uri'], f'select id from public."{table}"')


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_starts_with_its_parents_roles_and_then_keeps_its_own(engine_api, lived, request):
    api = engine_api
    project, database = lived['project'], lived['database']
    before = role_name('before')
    theirs = make(api, lived, before, access='read').json()
    operation = create_branch(api, project, database, name_for('copy')).json()
    child = at(project, database, finished(api, project, operation)['target']['id'])
    after = role_name('after')
    make(api, lived, after)

    names = [r['name'] for r in roles(api, child)]
    assert before in names and after not in names
    copied = role(api, child, before).json()
    assert (copied['access'], copied['login']) == ('read', True)
    # The same password, at the child's own address.
    port = api.call('getDatabaseConnection', path={'project': project, 'database': database},
                    params={'branch': child['branch']}, expect=200).json()['port']
    there = re.sub(r'127\.0\.0\.1:\d+', f'127.0.0.1:{port}', theirs['uri'])
    assert there != theirs['uri'] and ask(request, there, 'select amount from public.ledger where id = 1') == (True, '10')

    remove(api, child, before)
    assert before in [r['name'] for r in roles(api, lived)], 'deleting on the child leaves the parent alone'
    assert ask(request, theirs['uri'], 'select 1') == (True, '1')


@pytest.mark.engine
@pytest.mark.l2
def test_an_expired_branch_admits_no_role_and_waits_for_the_sessions_of_all(engine_api, lived, request):
    from test_branches import later, read as read_branch, update as update_branch
    api = engine_api
    project, database = lived['project'], lived['database']
    operation = create_branch(api, project, database, name_for('brief')).json()
    branch = finished(api, project, operation)['target']['id']
    child = at(project, database, branch)
    uri = make(api, child, role_name('guest'), access='read').json()['uri']
    binary = pathlib.Path(request.config.getoption('--bundle')) / 'pg_install/v17/bin/psql'
    working = subprocess.Popen([str(binary), uri, '-qAt', '-c', 'select pg_sleep(25)', '-c', 'select 7'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wait(lambda: role(api, child, uri.split('//')[1].split(':')[0]).json()['connections'] == 1)
    update_branch(api, project, database, branch, {'expires_at': later(3)})

    # Past its expiry the branch lets nobody new in, whatever role they hold.
    def shut():
        return not ask(request, uri, 'select 1')[0]
    wait(shut, timeout=60)
    # It is not deleted under a session that was already open, the role's included.
    assert working.poll() is None
    assert read_branch(api, project, database, branch, expect=None).status_code == 200
    out, err = working.communicate(timeout=90)
    assert (working.returncode, out.split()[-1]) == (0, '7'), err
    wait(lambda: read_branch(api, project, database, branch, expect=None).status_code == 404, timeout=120)
