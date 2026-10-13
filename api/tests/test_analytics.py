"""The analytics domain: scenarios (L2) and parity (L6).

A version is one published state of a branch's analytical copy, a session is
an engine reading one version, and a query is a statement run in a session.
Without the database engines nothing can be published or opened, so the tests
here that need neither cover what is refused and what an empty branch
reports. The tests marked `engine` and `analytics` publish real versions, open
real sessions and read what a query returns.

These tests run on a server of their own, like the database tests.
"""
import re
import uuid

import pytest

from harness.server import wait
from test_branches import address, first
from test_databases import (api, create as create_database, delete as delete_database, everything as databases, finished, key, name_for,  # noqa: F401
                            new_project, own, project, psql, read as read_database, runtime_of)

VERSION = re.compile(r'^ver_[0-9a-f]{32}$')
SESSION = re.compile(r'^ses_[0-9a-f]{32}$')
LSN = re.compile(r'^[0-9A-F]+/[0-9A-F]+$')
NONE = {name: prefix + '_' + '0' * 32
        for name, prefix in dict(version='ver', pin='pin', refresh='rfr', session='ses', branch='br').items()}
API = 1  # the command line's application API version


def post(api, operation, path, json=None, expect=None, used=None):
    return api.call(operation, path=path, json=json, headers={'Idempotency-Key': used or key()}, expect=expect)


def versions(api, project, branch, **params):
    items, cursor = [], None
    while True:
        page = api.call('listAnalyticalVersions', path={'project': project, 'branch': branch},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


def sessions(api, project, **params):
    items, cursor = [], None
    while True:
        page = api.call('listAnalyticalSessions', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


@pytest.fixture(scope='module')
def branch(api, project):
    return first(create_database(api, project, name_for('analytics')).json()['target']['id'])


@pytest.fixture(scope='module')
def elsewhere(api):
    """Another project, for what must not cross from one to the next."""
    return new_project(api)


# ---- L2: without engines ----

@pytest.mark.l2
def test_a_branch_nothing_was_synced_from_has_no_versions_and_no_sessions(api, project, branch):
    assert versions(api, project, branch) == []
    assert sessions(api, project) == [] and sessions(api, project, active='true') == []
    capacity = api.call('getAnalyticalCapacity', path={'project': project}, expect=200).json()
    assert capacity == {'session_slots': 2, 'sessions_open': 0}


@pytest.mark.l2
def test_collecting_a_branch_with_no_versions_deletes_nothing(api, project, branch):
    path = {'project': project, 'branch': branch}
    used = key()
    done = post(api, 'collectAnalyticalVersions', path, {'keep': 3}, expect=200, used=used).json()
    assert done == {'branch_id': branch, 'keep': 3, 'deleting': []}
    assert post(api, 'collectAnalyticalVersions', path, {'keep': 3}, expect=200, used=used).json() == done
    reused = post(api, 'collectAnalyticalVersions', path, {'keep': 4}, expect=409, used=used).json()
    assert reused['code'] == 'idempotency_key_reused'


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'keep': 0}, {'keep': 101}, {'keep': '3'}, {'keep': 3, 'all': True}])
def test_collect_refuses_a_keep_it_cannot_honour(api, project, branch, body):
    refused = post(api, 'collectAnalyticalVersions', {'project': project, 'branch': branch}, body, expect=400).json()
    assert refused['code'] == 'invalid_request'


@pytest.mark.l2
def test_a_change_needs_an_idempotency_key(api, project, branch):
    refused = api.call('collectAnalyticalVersions', path={'project': project, 'branch': branch},
                       json={'keep': 3}, expect=400).json()
    assert 'Idempotency-Key' in refused['detail']
    refused = api.call('openAnalyticalSession', path={'project': project}, json={'branch_id': branch}, expect=400).json()
    assert 'Idempotency-Key' in refused['detail']


@pytest.mark.l2
def test_publishing_and_reading_need_the_engines(api, project, branch):
    refresh = post(api, 'startAnalyticalRefresh', {'project': project, 'branch': branch}, expect=503).json()
    assert refresh['code'] == 'unavailable' and 'engines' in refresh['detail']
    session = post(api, 'openAnalyticalSession', {'project': project}, {'branch_id': branch}, expect=503).json()
    assert session['code'] == 'unavailable'
    assert sessions(api, project) == []


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'branch_id': NONE['branch'], 'version_id': NONE['version']}, {'branch_id': 7}, {'ttl_seconds': 60},
    {'branch_id': 'BRANCH', 'ttl_seconds': 9}, {'branch_id': 'BRANCH', 'ttl_seconds': 3601},
    {'branch_id': 'BRANCH', 'ttl_seconds': '60'}, {'branch_id': 'BRANCH', 'catalog': True},
])
def test_open_refuses_a_request_it_cannot_honour(api, project, branch, body):
    body = {name: branch if value == 'BRANCH' else value for name, value in body.items()}
    refused = post(api, 'openAnalyticalSession', {'project': project}, body, expect=400).json()
    assert refused['code'] == 'invalid_request'


@pytest.mark.l2
def test_a_session_is_opened_on_a_branch_or_version_of_the_project(api, project, elsewhere):
    theirs = databases(api, elsewhere)[0]['default_branch_id']
    for body in [{'branch_id': theirs}, {'branch_id': NONE['branch']}, {'branch_id': 'main'},
                 {'version_id': NONE['version']}, {'version_id': 'latest'}]:
        refused = post(api, 'openAnalyticalSession', {'project': project}, body, expect=404).json()
        assert refused['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('operation, name, method', [
    ('listAnalyticalVersions', 'branch', 'get'), ('collectAnalyticalVersions', 'branch', 'post'),
    ('startAnalyticalRefresh', 'branch', 'post'), ('getAnalyticalRefresh', 'refresh', 'get'),
    ('cancelAnalyticalRefresh', 'refresh', 'post'), ('getAnalyticalVersion', 'version', 'get'),
    ('pinAnalyticalVersion', 'version', 'post'), ('renewAnalyticalPin', 'pin', 'post'),
    ('releaseAnalyticalPin', 'pin', 'post'), ('getAnalyticalSession', 'session', 'get'),
    ('closeAnalyticalSession', 'session', 'post'), ('runAnalyticalQuery', 'session', 'post'),
    ('getAnalyticalQuery', 'session', 'get'),
])
@pytest.mark.parametrize('shape', ['unknown', 'malformed'])
def test_what_does_not_exist_is_not_found(api, project, operation, name, method, shape):
    body = {'collectAnalyticalVersions': {'keep': 3}, 'pinAnalyticalVersion': {'ttl_seconds': 60},
            'renewAnalyticalPin': {'ttl_seconds': 60}, 'runAnalyticalQuery': {'sql': 'SELECT 1'}}.get(operation)
    path = {'project': project, name: NONE[name] if shape == 'unknown' else 'nothing_' + uuid.uuid4().hex}
    if method == 'get':
        found = api.call(operation, path=path, expect=404)
    else:
        found = post(api, operation, path, body, expect=404)
    assert found.json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_branch_belongs_to_one_project(api, project, branch, elsewhere):
    path = {'project': elsewhere, 'branch': branch}
    assert api.call('listAnalyticalVersions', path=path, expect=404).json()['code'] == 'not_found'
    assert post(api, 'collectAnalyticalVersions', path, {'keep': 3}, expect=404).json()['code'] == 'not_found'
    assert post(api, 'startAnalyticalRefresh', path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_listing_refuses_what_it_did_not_issue(api, project, branch):
    for params in [{'cursor': 'zz'}, {'cursor': '5b5d'}, {'limit': 0}]:
        api.call('listAnalyticalVersions', path={'project': project, 'branch': branch}, params=params, expect=400)
        api.call('listAnalyticalSessions', path={'project': project}, params=params, expect=400)
    api.call('listAnalyticalSessions', path={'project': project}, params={'active': 'yes'}, expect=400)


# ---- With database engines and the analytical runtime ----

@pytest.fixture(scope='module')
def live(engine_api, request):
    """A running database that holds one table, as (project, database, first
    branch, URI). It is in the project the server's checkout is bound to,
    which is the one the command line works in."""
    project = next(p['id'] for p in engine_api.call('listProjects', expect=200).json()['items'] if p['name'] == 'api-tests')
    database = finished(engine_api, project, create_database(engine_api, project, name_for('orders')).json())['target']['id']
    uri = address(engine_api, project, database, first(database))
    psql(request, uri, "create table public.orders(id bigint primary key, note text); "
                       "insert into orders select n, 'n' || n from generate_series(1, 50) n")
    # It is the first database of that project and so holds its default
    # branch, which is why it is not deleted afterwards.
    return project, database, first(database), uri


def dropped(api, project, database):
    """Delete a database: the engine server is shared, and it runs 32 branches."""
    name = read_database(api, project, database).json()['name']
    assert finished(api, project, delete_database(api, project, database, name, '*').json())['state'] == 'succeeded'


def refreshed(api, project, branch, timeout=240):
    """A refresh of the branch, once it has ended."""
    made = post(api, 'startAnalyticalRefresh', {'project': project, 'branch': branch}, expect=201)
    refresh = made.json()
    assert refresh['state'] == 'running' and refresh['version_id'] is None and refresh['branch_id'] == branch
    assert made.headers['Location'].endswith(f"/projects/{project}/analytics/refreshes/{refresh['id']}")

    def final():
        found = api.call('getAnalyticalRefresh', path={'project': project, 'refresh': refresh['id']}, expect=200).json()
        return found if found['state'] != 'running' else None
    return wait(final, timeout=timeout)


def session_in(api, project, session, state, timeout=120):
    def reached():
        found = api.call('getAnalyticalSession', path={'project': project, 'session': session}, expect=200).json()
        assert found['state'] != 'failed' or state == 'failed', found
        return found if found['state'] == state else None
    return wait(reached, timeout=timeout)


def answered(api, project, session, sql, timeout=60, **limits):
    """A query run in a session, once it has ended."""
    path = {'project': project, 'session': session}
    query = post(api, 'runAnalyticalQuery', path, {'sql': sql, **limits}, expect=201).json()
    assert query['state'] == 'running' and query['columns'] is None and query['rows'] is None

    def final():
        found = api.call('getAnalyticalQuery', path=path, expect=200).json()
        assert found['id'] == query['id']
        return found if found['state'] != 'running' else None
    return wait(final, timeout=timeout)


def closed(api, project, session):
    post(api, 'closeAnalyticalSession', {'project': project, 'session': session}, expect=200)
    return session_in(api, project, session, 'closed')


@pytest.mark.engine
@pytest.mark.analytics
@pytest.mark.l2
@pytest.mark.l6
def test_refreshes_publish_versions_that_are_pinned_and_collected(engine_server, engine_api, live, request):
    api, (project, database, branch, uri) = engine_api, live
    assert versions(api, project, branch) == []
    done = refreshed(api, project, branch)
    assert done['state'] == 'succeeded' and done['error'] is None and VERSION.match(done['version_id']), done
    refused = post(api, 'cancelAnalyticalRefresh', {'project': project, 'refresh': done['id']}, expect=409).json()
    assert 'published' in refused['detail']

    [version] = versions(api, project, branch)
    assert version['id'] == done['version_id'] and version['current'] is True and version['state'] == 'available'
    assert version['project_id'] == project and version['database_id'] == database and version['branch_id'] == branch
    assert version['pipeline_id'] is None and version['error'] is None and version['published_at']
    assert LSN.match(version['source_lsn'])
    assert version['table_count'] == 1 and version['row_count'] == 50 and version['size_bytes'] > 0

    detail = api.call('getAnalyticalVersion', path={'project': project, 'version': version['id']}, expect=200).json()
    assert {name: detail[name] for name in version} == version
    [table] = detail['tables']
    assert (table['schema'], table['name'], table['rows']) == ('public', 'orders', 50)
    assert [column['name'] for column in table['columns']] == ['id', 'note']
    assert all(column['type'] for column in table['columns'])
    assert detail['observed_at'] and detail['readers'] == {'session_ids': [], 'pins': []}
    assert all(set(relation) == {'schema', 'name', 'reason'} for relation in detail['omitted_tables'])

    # The command line names the same version as the branch's current one.
    internal = lambda public: str(uuid.UUID(public.split('_')[1]))  # noqa: E731
    theirs = engine_server.rpc(method='api', api_version=API,
                               binding=engine_server.binding,
                               action=dict(action='current_snapshot', branch=internal(branch)))
    assert runtime_of(engine_server, database) == engine_server.binding['project_id']
    assert theirs['publication']['epoch_id'] == internal(version['id'])

    # A second refresh publishes what the branch holds then, and becomes current.
    psql(request, uri, "insert into orders values (51, 'later')")
    second = refreshed(api, project, branch)
    newer, older = versions(api, project, branch)
    assert (newer['id'], older['id']) == (second['version_id'], version['id'])
    assert (newer['current'], older['current']) == (True, False)
    assert newer['row_count'] == 51 and newer['sequence'] > older['sequence']
    page = api.call('listAnalyticalVersions', path={'project': project, 'branch': branch},
                    params={'limit': 1}, expect=200).json()
    assert [v['id'] for v in page['items']] == [newer['id']] and page['next_cursor']

    # A pin holds a version that collecting would otherwise delete.
    old = {'project': project, 'version': older['id']}
    made = post(api, 'pinAnalyticalVersion', old, {'ttl_seconds': 600}, expect=201)
    pin = made.json()
    assert pin['version_id'] == older['id'] and made.headers['Location'].endswith('/analytics/pins/' + pin['id'])
    assert api.call('getAnalyticalVersion', path=old, expect=200).json()['readers']['pins'] == [pin]
    for body in [{}, {'ttl_seconds': 0}, {'ttl_seconds': 86401}, {'ttl_seconds': 60, 'forever': True}]:
        post(api, 'pinAnalyticalVersion', old, body, expect=400)
    kept = post(api, 'collectAnalyticalVersions', {'project': project, 'branch': branch}, {'keep': 1}, expect=200).json()
    assert kept['deleting'] == []
    one = {'project': project, 'pin': pin['id']}
    longer = post(api, 'renewAnalyticalPin', one, {'ttl_seconds': 3600}, expect=200).json()
    assert longer['id'] == pin['id'] and longer['expires_at'] > pin['expires_at']
    post(api, 'releaseAnalyticalPin', one, expect=204)
    post(api, 'releaseAnalyticalPin', one, expect=404)
    post(api, 'renewAnalyticalPin', one, {'ttl_seconds': 60}, expect=404)

    collected = post(api, 'collectAnalyticalVersions', {'project': project, 'branch': branch}, {'keep': 1}, expect=200).json()
    assert collected['deleting'] == [older['id']]
    # Its files go a moment later, and then it is no longer a version.
    wait(lambda: api.call('getAnalyticalVersion', path=old).status_code == 404, timeout=60)
    assert [v['id'] for v in versions(api, project, branch)] == [newer['id']]
    post(api, 'pinAnalyticalVersion', old, {'ttl_seconds': 60}, expect=404)


@pytest.mark.engine
@pytest.mark.analytics
@pytest.mark.l2
def test_sessions_read_a_version_and_share_two_slots(engine_api, live, request):
    api, (project, database, branch, uri) = engine_api, live
    current = versions(api, project, branch)
    if not current:
        refreshed(api, project, branch)
        current = versions(api, project, branch)
    version = current[0]
    rows = version['row_count']
    opened = []
    try:
        used = key()
        made = post(api, 'openAnalyticalSession', {'project': project}, {'branch_id': branch, 'ttl_seconds': 600},
                    expect=201, used=used)
        session = made.json()
        opened.append(session['id'])
        assert SESSION.match(session['id']) and session['state'] in ('starting', 'ready')
        assert made.headers['Location'].endswith('/analytics/sessions/' + session['id'])
        assert (session['project_id'], session['database_id'], session['branch_id']) == (project, database, branch)
        assert session['version_id'] == version['id'] and session['refresh_id'] is None
        assert session['purpose'] == 'query' and session['error'] is None
        again = post(api, 'openAnalyticalSession', {'project': project}, {'branch_id': branch, 'ttl_seconds': 600},
                     expect=201, used=used).json()
        assert again['id'] == session['id']
        if session['state'] == 'starting':
            assert session['spark_connect_url'] is None

        ready = session_in(api, project, session['id'], 'ready')
        assert ready['spark_connect_url'].startswith('sc://')
        path = {'project': project, 'session': session['id']}
        assert api.call('getAnalyticalQuery', path=path, expect=404).json()['code'] == 'not_found'
        detail = api.call('getAnalyticalVersion', path={'project': project, 'version': version['id']}, expect=200).json()
        assert detail['readers']['session_ids'] == [session['id']]

        counted = answered(api, project, session['id'], 'SELECT count(*) AS n, max(note) AS last FROM orders')
        assert counted['state'] == 'complete' and counted['error'] is None and counted['truncated'] is False, counted
        assert [column['name'] for column in counted['columns']] == ['n', 'last']
        assert counted['rows'] == [[str(rows), 'n9']]
        assert counted['session_id'] == session['id'] and counted['version_id'] == version['id']

        # The session keeps reading its version while the branch moves on.
        psql(request, uri, "insert into orders values (1000, 'unseen')")
        assert answered(api, project, session['id'], 'SELECT count(*) FROM orders')['rows'] == [[str(rows)]]
        some = answered(api, project, session['id'], 'SELECT id FROM orders ORDER BY id', max_rows=3)
        assert some['rows'] == [['1'], ['2'], ['3']] and some['truncated'] is True
        wrong = answered(api, project, session['id'], 'SELECT nothing FROM nowhere')
        assert wrong['state'] == 'failed' and wrong['error'] and wrong['rows'] is None and wrong['sql'] is None
        written = answered(api, project, session['id'], "INSERT INTO orders VALUES (2000, 'no')")
        assert written['state'] == 'failed', 'a session is read-only'
        for body in [{}, {'sql': ''}, {'sql': 7}, {'sql': 'SELECT 1', 'max_rows': 1001},
                     {'sql': 'SELECT 1', 'timeout_ms': 99}, {'sql': 'SELECT 1', 'explain': True}]:
            post(api, 'runAnalyticalQuery', path, body, expect=400)

        # A second session on the version by name takes the other slot.
        other = post(api, 'openAnalyticalSession', {'project': project}, {'version_id': version['id']}, expect=201).json()
        opened.append(other['id'])
        assert other['version_id'] == version['id'] and other['branch_id'] == branch
        full = post(api, 'openAnalyticalSession', {'project': project}, {'branch_id': branch}, expect=409).json()
        assert full['code'] == 'capacity_exhausted'
        capacity = api.call('getAnalyticalCapacity', path={'project': project}, expect=200).json()
        assert capacity == {'session_slots': 2, 'sessions_open': 2}
        assert [s['id'] for s in sessions(api, project, active='true')] == [other['id'], session['id']]
        assert [s['id'] for s in sessions(api, project, limit=1)][:2] == [other['id'], session['id']]
        # A version a session reads is not collected.
        psql(request, uri, "insert into orders values (1001, 'newer')")
        refreshed(api, project, branch)
        held = post(api, 'collectAnalyticalVersions', {'project': project, 'branch': branch}, {'keep': 1}, expect=200).json()
        assert version['id'] not in held['deleting']

        closing = post(api, 'closeAnalyticalSession', path, expect=200).json()
        assert closing['state'] in ('closing', 'closed') and closing['spark_connect_url'] is None
        ended = session_in(api, project, session['id'], 'closed')
        assert ended['error'] == 'closed'
        assert post(api, 'closeAnalyticalSession', path, expect=200).json()['state'] == 'closed'
        late = post(api, 'runAnalyticalQuery', path, {'sql': 'SELECT 1'}, expect=409).json()
        assert late['code'] == 'conflict'
        assert [s['id'] for s in sessions(api, project, active='true')] == [other['id']]
        assert {s['id'] for s in sessions(api, project)} >= {other['id'], session['id']}
        assert api.call('getAnalyticalCapacity', path={'project': project}, expect=200).json()['sessions_open'] == 1
    finally:
        for session in opened:
            closed(api, project, session)


@pytest.mark.engine
@pytest.mark.analytics
@pytest.mark.l2
def test_a_session_on_a_branch_with_no_version_waits_for_one(engine_api, live, request):
    api, (project, _, _, _) = engine_api, live
    database = finished(api, project, create_database(api, project, name_for('fresh')).json())['target']['id']
    branch = first(database)
    psql(request, address(api, project, database, branch), 'create table public.things(id int primary key); '
                                                           'insert into things values (1), (2)')
    # A refresh that is cancelled publishes nothing.
    started = post(api, 'startAnalyticalRefresh', {'project': project, 'branch': branch}, expect=201).json()
    stopped = post(api, 'cancelAnalyticalRefresh', {'project': project, 'refresh': started['id']}, expect=200).json()
    assert stopped['state'] == 'cancelled' and stopped['version_id'] is None and stopped['error'] == 'cancelled'
    again = post(api, 'cancelAnalyticalRefresh', {'project': project, 'refresh': started['id']}, expect=200).json()
    assert again == stopped
    assert versions(api, project, branch) == []

    # An installation makes one copy at a time, and the cancelled one is still being cleared away.
    def opened():
        made = post(api, 'openAnalyticalSession', {'project': project}, {'branch_id': branch})
        assert made.status_code in (201, 409), made.text
        return made.json() if made.status_code == 201 else None
    session = wait(opened, timeout=120)
    try:
        assert session['state'] == 'waiting' and session['version_id'] is None and session['refresh_id']
        ready = session_in(api, project, session['id'], 'ready', timeout=240)
        [version] = versions(api, project, branch)
        assert ready['version_id'] == version['id']
        refresh = api.call('getAnalyticalRefresh', path={'project': project, 'refresh': session['refresh_id']},
                           expect=200).json()
        assert refresh['state'] == 'succeeded' and refresh['version_id'] == version['id']
        assert answered(api, project, session['id'], 'SELECT count(*) FROM things')['rows'] == [['2']]
    finally:
        closed(api, project, session['id'])
    dropped(api, project, database)
