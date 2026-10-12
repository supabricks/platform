"""The activity domain: scenarios (L2).

Sessions are what PostgreSQL says is connected, so without the database
engines the tests here cover only what is refused before it is asked. The
tests marked `engine` open real connections, block one on another, and stop
them through the API.
"""
import pathlib
import subprocess
import uuid

import pytest

from harness.server import wait
from test_branches import at, first
from test_databases import (api, change, everything as databases, finished, idle, key, name_for, own,  # noqa: F401
                            project, read as read_database)
from test_sql import live, values, write  # noqa: F401

NO_BRANCH = 'br_' + '0' * 32


def sessions(api, place, **params):
    found, cursor = [], None
    while True:
        page = api.call('listSessions', path=place, params={**params, **({'cursor': cursor} if cursor else {})},
                        expect=200).json()
        found += page['items']
        cursor = page['next_cursor']
        if not cursor:
            return found


def stop(api, place, pid, how='cancelSession', expect=204):
    return api.call(how, path={**place, 'pid': pid}, expect=expect)


@pytest.fixture(scope='module')
def place(api, project):
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    return at(project, database, first(database))


# ---- L2: what is refused before PostgreSQL is asked ----

@pytest.mark.l2
def test_sessions_need_the_database_engines(api, place):
    for response in [api.call('listSessions', path=place, expect=503), stop(api, place, 4242, expect=503),
                     stop(api, place, 4242, 'terminateSession', expect=503)]:
        assert response.json()['code'] == 'unavailable'


@pytest.mark.l2
def test_sessions_belong_to_a_branch_that_exists(api, place):
    nowhere = {**place, 'branch': NO_BRANCH}
    for response in [api.call('listSessions', path=nowhere, expect=404), stop(api, nowhere, 4242, expect=404),
                     stop(api, nowhere, 4242, 'terminateSession', expect=404)]:
        assert response.json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('pid', ['abc', '0', '-1', '1.5', '99999999999', '1;select 1'])
def test_a_session_is_named_by_a_process_id(api, place, pid):
    for how in ('cancel', 'terminate'):
        response = api.raw('POST', f"/projects/{place['project']}/databases/{place['database']}"
                                   f"/branches/{place['branch']}/sessions/{pid}/{how}")
        assert response.status_code == 404, response.text


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'limit': '0'}, {'limit': 'x'}, {'cursor': 'zz'},
                                    {'cursor': '5b2261225d'}, {'cursor': '5b2231222c2232225d'}])
def test_listing_sessions_rejects_bad_paging(api, place, params):
    assert api.call('listSessions', path=place, params=params, expect=400).json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_statement_statistics_are_not_served(api, place):
    response = api.raw('GET', f"/projects/{place['project']}/databases/{place['database']}"
                              f"/branches/{place['branch']}/statement-statistics")
    assert response.status_code == 404


# ---- With database engines ----

@pytest.fixture(scope='module')
def lived(engine_api, live):
    project, database, branch = live
    write(engine_api, project, branch, 'create table if not exists public.held (id int)')
    return at(project, database, branch)


@pytest.fixture
def connect(engine_api, lived, request):
    """Open a connection as the application role that runs the SQL given, and
    close whatever is left open when the test ends."""
    uri = engine_api.call('revealDatabaseCredentials', path={'project': lived['project'],
                                                             'database': lived['database']},
                          json={'branch_id': lived['branch']}, expect=200).json()['uri']
    binary = pathlib.Path(request.config.getoption('--bundle')) / 'pg_install/v17/bin/psql'
    opened = []

    def run(*sql, name=None):
        name = name or 'test-' + uuid.uuid4().hex[:8]
        commands = [part for statement in sql for part in ('-c', statement)]
        process = subprocess.Popen([str(binary), uri, '-qAt', *commands], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env={'PGAPPNAME': name})
        opened.append(process)
        return name, process
    yield run
    for process in opened:
        process.kill()
        process.wait()


def seen(api, place, name, **wanted):
    """The session a test opened, once it looks as wanted."""
    def look():
        found = [s for s in sessions(api, place) if s['application'] == name]
        return found[0] if found and all(found[0][k] == v for k, v in wanted.items()) else None
    return wait(look, timeout=60)


@pytest.mark.engine
@pytest.mark.l2
def test_a_session_is_listed_with_what_it_is_doing(engine_api, lived, connect):
    api = engine_api
    name, _ = connect("select pg_sleep(60) /* watched */")
    found = seen(api, lived, name, state='active')
    assert (found['role'], found['query']) == ('supabricks_owner', 'select pg_sleep(60) /* watched */')
    assert found['pid'] > 0 and found['duration_ms'] >= 0
    assert (found['blocked_by'], found['waiting_for']) == ([], None)
    assert found['connected_at'] <= found['query_started_at'] and found['query_started_at'].endswith('Z')
    assert found['transaction_started_at'] is not None
    later = seen(api, lived, name, state='active')
    assert later['pid'] == found['pid'] and later['duration_ms'] >= found['duration_ms']

    # The server's own connections are not sessions anyone opened.
    listed = sessions(api, lived)
    assert all(s['role'] != 'cloud_admin' for s in listed)
    pids = [s['pid'] for s in listed]
    assert pids == sorted(pids)
    assert [s['pid'] for s in sessions(api, lived, limit=1) if s['pid'] in pids] == pids


@pytest.mark.engine
@pytest.mark.l2
def test_cancel_stops_the_statement_and_keeps_the_connection(engine_api, lived, connect):
    api = engine_api
    name, process = connect('select pg_sleep(60)', 'select pg_sleep(60)')
    found = seen(api, lived, name, state='active')
    stop(api, lived, found['pid'])
    # The first statement is cancelled; the same connection goes on to the second.
    again = seen(api, lived, name, state='active')
    assert again['pid'] == found['pid'] and process.poll() is None
    stop(api, lived, found['pid'], 'terminateSession')
    assert process.wait(timeout=30) != 0
    assert 'canceling statement due to user request' in process.stderr.read()


@pytest.mark.engine
@pytest.mark.l2
def test_terminate_closes_the_connection(engine_api, lived, connect):
    api = engine_api
    name, process = connect('select pg_sleep(60)')
    pid = seen(api, lived, name, state='active')['pid']
    stop(api, lived, pid, 'terminateSession')
    assert process.wait(timeout=30) != 0
    wait(lambda: pid not in [s['pid'] for s in sessions(api, lived)])
    assert stop(api, lived, pid, 'terminateSession', expect=404).json()['code'] == 'not_found'
    assert stop(api, lived, pid, expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_a_blocked_session_names_who_blocks_it_and_on_what(engine_api, lived, connect):
    api = engine_api
    holder, _ = connect('begin', 'lock table public.held in access exclusive mode', 'select pg_sleep(60)')
    holding = seen(api, lived, holder, state='active')
    waiter, waiting = connect('select count(*) from public.held')
    blocked = seen(api, lived, waiter, wait_event_type='Lock')
    assert blocked['blocked_by'] == [holding['pid']]
    assert blocked['waiting_for'] == {'lock': 'relation', 'mode': 'AccessShareLock', 'relation': 'public.held'}
    assert (blocked['state'], blocked['wait_event']) == ('active', 'relation')
    assert seen(api, lived, holder)['blocked_by'] == []

    stop(api, lived, holding['pid'], 'terminateSession')
    out, err = waiting.communicate(timeout=30)
    assert (waiting.returncode, out.strip()) == (0, '0'), err


@pytest.mark.engine
@pytest.mark.l2
def test_the_servers_own_sessions_cannot_be_stopped(engine_api, lived):
    api = engine_api
    project, branch = lived['project'], lived['branch']
    own = values(api, project, branch, "select pid from pg_stat_activity where usename = 'cloud_admin' "
                                       "and backend_type = 'client backend' limit 1")
    background = values(api, project, branch, "select pid from pg_stat_activity where backend_type = 'checkpointer'")
    for pid in [row[0] for row in own + background]:
        assert stop(api, lived, int(pid), expect=404).json()['code'] == 'not_found'
        assert stop(api, lived, int(pid), 'terminateSession', expect=404).json()['code'] == 'not_found'
    assert values(api, project, branch, 'select 1') == [['1']], 'the branch still answers'


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_that_is_not_running_has_no_sessions_and_is_not_woken(engine_api):
    api = engine_api
    made = api.call('createProject', json={'name': name_for('asleep')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    place = at(project, database, first(database))
    idle(api, project, database)
    finished(api, project, change(api, 'suspendDatabase', project, database).json())
    assert sessions(api, place) == []
    assert stop(api, place, 4242, expect=404).json()['code'] == 'not_found'
    assert read_database(api, project, database).json()['state'] == 'suspended'
