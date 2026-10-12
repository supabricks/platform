"""The SQL domain: scenarios (L2), durability (L5) and parity (L6).

A statement runs on a branch through the database engines, so without them
the tests here cover what is refused before anything runs, and saved queries
in full: they are files and need no engine. The tests marked `engine` run
statements on a real PostgreSQL: results, errors, limits, cancelling, the four
workers and waking a suspended branch.

These tests run on a server of their own, like the database tests.
"""
import json
import re
import stat
import uuid

import pytest

from harness.server import wait
from test_branches import first
from test_databases import (api, change as change_database, everything as databases, finished, idle,  # noqa: F401
                            key, name_for, new_project, own, project, read as read_database, runtime_of)

STATEMENT_ID = re.compile(r'^stmt_[0-9a-f]{32}$')
QUERY_ID = re.compile(r'^qry_[0-9a-f]{32}$')
NO_STATEMENT = 'stmt_' + '0' * 32
NO_QUERY = 'qry_' + '0' * 32
NO_BRANCH = 'br_' + '0' * 32
FINAL = ('succeeded', 'failed', 'cancelled')


def main_of(api, project):
    """The first branch of a project's `main` database."""
    return first(next(d for d in databases(api, project) if d['name'] == 'main')['id'])


@pytest.fixture(scope='module')
def branch(api, project):
    return main_of(api, project)


def run(api, project, branch, sql, expect=201, used=None, **extra):
    return api.call('runStatement', path={'project': project}, json={'branch_id': branch, 'sql': sql, **extra},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def statement(api, project, id, expect=200):
    return api.call('getStatement', path={'project': project, 'statement': id}, expect=expect)


def cancel(api, project, id, expect=200):
    return api.call('cancelStatement', path={'project': project, 'statement': id}, expect=expect)


def done(api, project, started, timeout=60):
    """The statement once it has finished."""
    def final():
        found = statement(api, project, started['id']).json()
        return found if found['state'] in FINAL else None
    return wait(final, timeout=timeout)


def save(api, project, branch, title=None, sql='select 1', expect=201, used=None):
    return api.call('createSavedQuery', path={'project': project},
                    json={'title': title or name_for('query'), 'sql': sql, 'branch_id': branch},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def saved(api, project, id, expect=200):
    return api.call('getSavedQuery', path={'project': project, 'query': id}, expect=expect)


def change(api, project, id, body, etag=None, expect=200):
    return api.call('updateSavedQuery', path={'project': project, 'query': id}, json=body,
                    headers={'If-Match': etag} if etag else {}, expect=expect)


def remove(api, project, id, etag=None, expect=204):
    return api.call('deleteSavedQuery', path={'project': project, 'query': id},
                    headers={'If-Match': etag} if etag else {}, expect=expect)


def everything(api, project, **params):
    """Every saved query of a project, following cursors."""
    items, cursor = [], None
    while True:
        page = api.call('listSavedQueries', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


# ---- L2: statements, before anything runs ----

@pytest.mark.l2
def test_a_statement_needs_the_database_engines(api, project, branch):
    used = key()
    for _ in range(2):
        refused = run(api, project, branch, 'select 1', expect=503, used=used).json()
        assert refused['code'] == 'unavailable', 'a refusal is not remembered as the reply to the key'


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'sql': 'select 1'}, {'branch_id': NO_BRANCH}, {'sql': '', 'branch_id': NO_BRANCH},
    {'sql': ' \n\t', 'branch_id': NO_BRANCH}, {'sql': 'x' * 32769, 'branch_id': NO_BRANCH},
    {'sql': 1, 'branch_id': NO_BRANCH}, {'sql': 'select 1', 'branch_id': 7},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'read_only': 'yes'},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'max_rows': 0},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'max_rows': 1001},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'max_rows': 1.5},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'max_rows': '5'},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'timeout_ms': 99},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'timeout_ms': 30001},
    {'sql': 'select 1', 'branch_id': NO_BRANCH, 'database_id': 'db_' + '0' * 32},
    [], 'select 1',
], ids=repr)
def test_run_rejects_an_invalid_body(api, project, body):
    response = api.call('runStatement', path={'project': project}, json=body,
                        headers={'Idempotency-Key': key()}, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('named', [NO_BRANCH, 'main', 'db_' + '0' * 32, str(uuid.uuid4())])
def test_run_names_a_branch_of_the_project(api, project, named):
    assert run(api, project, named, 'select 1', expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_branch_of_another_project_is_not_run_on(api, project, branch):
    other = new_project(api)
    assert run(api, other, branch, 'select 1', expect=404).json()['code'] == 'not_found'
    assert run(api, 'prj_' + '0' * 32, branch, 'select 1', expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('headers', [{}, {'Idempotency-Key': 'short'}, {'Idempotency-Key': 'has space in it'}])
def test_run_requires_a_well_formed_idempotency_key(api, project, branch, headers):
    response = api.call('runStatement', path={'project': project}, json={'branch_id': branch, 'sql': 'select 1'},
                        headers=headers, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('id', [NO_STATEMENT, 'nonsense', 'qry_' + '0' * 32, str(uuid.uuid4())])
def test_an_unknown_or_malformed_statement_is_404(api, project, id):
    assert statement(api, project, id, expect=404).json()['code'] == 'not_found'
    assert cancel(api, project, id, expect=404).json()['code'] == 'not_found'


# ---- L2: saved queries ----

@pytest.mark.l2
def test_save_returns_the_query_and_where_to_read_it(api, project, branch):
    title = name_for('Customers without an email')
    response = save(api, project, branch, title=title, sql='select id from customers\nwhere email is null')
    made = response.json()
    assert QUERY_ID.match(made['id'])
    assert made['title'] == title and made['sql'] == 'select id from customers\nwhere email is null'
    assert made['branch_id'] == branch and made['revision'] == 1
    assert response.headers['ETag'] == '"1"'
    assert response.headers['Location'] == f'/api/v1/projects/{project}/saved-queries/{made["id"]}'
    read = saved(api, project, made['id'])
    assert read.json() == made and read.headers['ETag'] == '"1"'
    assert made in everything(api, project)


@pytest.mark.l2
def test_save_with_the_same_key_returns_the_first_reply(api, project, branch):
    used, title = key(), name_for('once')
    made = save(api, project, branch, title=title, used=used)
    again = save(api, project, branch, title=title, used=used)
    assert again.json() == made.json() and again.status_code == 201
    assert [q['id'] for q in everything(api, project) if q['title'] == title] == [made.json()['id']]
    reused = save(api, project, branch, title=name_for('other'), used=used, expect=409)
    assert reused.json()['code'] == 'idempotency_key_reused'


@pytest.mark.l2
def test_titles_need_not_be_unique_and_sql_is_stored_as_written(api, project, branch):
    title, sql = name_for('same'), '  not sql at all; -- résumé ✓ "quoted" \\ \n'
    one, two = (save(api, project, branch, title=title, sql=sql).json() for _ in range(2))
    assert one['id'] != two['id'] and one['title'] == two['title'] == title
    assert saved(api, project, one['id']).json()['sql'] == sql


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'title': 't', 'sql': 'select 1'}, {'title': 't', 'branch_id': NO_BRANCH}, {'sql': 'select 1', 'branch_id': NO_BRANCH},
    {'title': '', 'sql': 'select 1', 'branch_id': NO_BRANCH}, {'title': '   ', 'sql': 'select 1', 'branch_id': NO_BRANCH},
    {'title': 't' * 121, 'sql': 'select 1', 'branch_id': NO_BRANCH},
    {'title': 'line\nbreak', 'sql': 'select 1', 'branch_id': NO_BRANCH},
    {'title': 't', 'sql': '', 'branch_id': NO_BRANCH}, {'title': 't', 'sql': 'x' * 32769, 'branch_id': NO_BRANCH},
    {'title': 7, 'sql': 'select 1', 'branch_id': NO_BRANCH},
    {'title': 't', 'sql': 'select 1', 'branch_id': NO_BRANCH, 'folder': 'reports'},
], ids=repr)
def test_save_rejects_an_invalid_body(api, project, body):
    response = api.call('createSavedQuery', path={'project': project}, json=body,
                        headers={'Idempotency-Key': key()}, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_a_title_is_counted_in_characters(api, project, branch):
    title = 'é' * 120
    assert save(api, project, branch, title=title).json()['title'] == title


@pytest.mark.l2
def test_sql_that_doubles_in_size_when_stored_is_kept_whole(api, project, branch):
    sql = '"' * 29000
    made = save(api, project, branch, sql=sql).json()
    assert saved(api, project, made['id']).json()['sql'] == sql


@pytest.mark.l2
def test_save_names_a_branch_of_the_project(api, project, branch):
    other = new_project(api)
    assert save(api, project, NO_BRANCH, expect=404).json()['code'] == 'not_found'
    assert save(api, other, branch, expect=404).json()['code'] == 'not_found'
    assert save(api, 'prj_' + '0' * 32, branch, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('headers', [{}, {'Idempotency-Key': 'short'}])
def test_save_requires_a_well_formed_idempotency_key(api, project, branch, headers):
    response = api.call('createSavedQuery', path={'project': project},
                        json={'title': 't', 'sql': 'select 1', 'branch_id': branch}, headers=headers, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_list_is_ordered_by_title_and_pages_without_gaps_or_repeats(api):
    project = new_project(api)
    branch = main_of(api, project)
    assert everything(api, project) == []
    titles = ['b', 'a', 'c', 'a', 'B']
    made = [save(api, project, branch, title=title).json() for title in titles]
    ordered = sorted(made, key=lambda q: (q['title'], q['id']))
    assert everything(api, project) == ordered
    assert everything(api, project, limit=2) == ordered
    assert everything(api, project, limit=1) == ordered


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'limit': '0'}, {'limit': '201'}, {'limit': 'x'}, {'cursor': 'zz'},
                                    {'cursor': '5b2261225d'}])
def test_list_rejects_bad_paging(api, project, params):
    response = api.call('listSavedQueries', path={'project': project}, params=params, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('id', [NO_QUERY, 'nonsense', 'stmt_' + '0' * 32, str(uuid.uuid4()), '..%2F..%2Fx'])
def test_an_unknown_or_malformed_saved_query_is_404(api, project, id):
    assert saved(api, project, id, expect=404).json()['code'] == 'not_found'
    assert change(api, project, id, {'title': 't'}, expect=404).json()['code'] == 'not_found'
    assert remove(api, project, id, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_saved_query_belongs_to_one_project_only(api, project, branch):
    made = save(api, project, branch).json()
    other = new_project(api)
    assert saved(api, other, made['id'], expect=404).json()['code'] == 'not_found'
    assert remove(api, other, made['id'], expect=404).json()['code'] == 'not_found'
    assert made['id'] not in [q['id'] for q in everything(api, other)]
    assert saved(api, project, made['id']).json() == made


@pytest.mark.l2
def test_a_change_raises_the_revision_and_no_change_does_not(api, project, branch):
    made = save(api, project, branch).json()
    title = name_for('renamed')
    changed = change(api, project, made['id'], {'title': title})
    assert changed.json() == {**made, 'title': title, 'revision': 2, 'updated_at': changed.json()['updated_at']}
    assert changed.headers['ETag'] == '"2"'
    same = change(api, project, made['id'], {'title': title, 'sql': made['sql'], 'branch_id': branch})
    assert same.json() == changed.json() and same.headers['ETag'] == '"2"'
    both = change(api, project, made['id'], {'sql': 'select 2', 'title': made['title']}).json()
    assert (both['sql'], both['title'], both['revision']) == ('select 2', made['title'], 3)
    assert saved(api, project, made['id']).json() == both


@pytest.mark.l2
def test_a_saved_query_can_move_to_another_branch(api, project, branch):
    other = first(api.call('createDatabase', path={'project': project}, json={'name': name_for()},
                           headers={'Idempotency-Key': key()}, expect=202).json()['target']['id'])
    made = save(api, project, branch).json()
    moved = change(api, project, made['id'], {'branch_id': other}).json()
    assert moved['branch_id'] == other and moved['revision'] == 2
    assert change(api, project, made['id'], {'branch_id': NO_BRANCH}, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_if_match_makes_a_change_conditional(api, project, branch):
    made = save(api, project, branch).json()
    change(api, project, made['id'], {'title': name_for('first')}, etag='"1"')
    stale = change(api, project, made['id'], {'title': name_for('second')}, etag='"1"', expect=412)
    assert stale.json()['code'] == 'revision_mismatch'
    assert change(api, project, made['id'], {'title': 't'}, etag='nonsense', expect=412).json()['code'] == 'revision_mismatch'
    assert saved(api, project, made['id']).json()['revision'] == 2
    assert change(api, project, made['id'], {'title': name_for('any')}, etag='*').json()['revision'] == 3


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'title': ''}, {'title': 't' * 121}, {'sql': ' '}, {'title': 't', 'revision': 9},
                                  {'branch_id': 5}, []], ids=repr)
def test_change_rejects_an_invalid_body(api, project, branch, body):
    made = save(api, project, branch).json()
    assert change(api, project, made['id'], body, expect=400).json()['code'] == 'invalid_request'
    assert saved(api, project, made['id']).json() == made


@pytest.mark.l2
def test_delete_removes_the_query_for_good(api, project, branch):
    made = save(api, project, branch).json()
    assert remove(api, project, made['id'], etag='"7"', expect=412).json()['code'] == 'revision_mismatch'
    gone = remove(api, project, made['id'], etag='"1"')
    assert gone.content == b''
    assert saved(api, project, made['id'], expect=404).json()['code'] == 'not_found'
    assert remove(api, project, made['id'], expect=404).json()['code'] == 'not_found'
    assert made['id'] not in [q['id'] for q in everything(api, project)]


@pytest.mark.l2
def test_a_project_keeps_at_most_100_saved_queries(api):
    project = new_project(api)
    branch = main_of(api, project)
    made = [save(api, project, branch, title=f'q{n:03}').json()['id'] for n in range(100)]
    full = save(api, project, branch, expect=409).json()
    assert full['code'] == 'capacity_exhausted'
    assert len(everything(api, project, limit=200)) == len(everything(api, project, limit=30)) == 100
    remove(api, project, made[0])
    save(api, project, branch)


# ---- L5: durability ----

@pytest.mark.l5
def test_saved_queries_and_replies_survive_a_killed_daemon(private_server):
    api = private_server.session()
    project = new_project(api)
    branch = main_of(api, project)
    used = key()
    made = save(api, project, branch, title='kept', used=used).json()
    changed = change(api, project, made['id'], {'sql': 'select 2'}).json()
    dropped = save(api, project, branch).json()
    remove(api, project, dropped['id'])
    private_server.restart(kill=True)
    api = private_server.session()
    assert everything(api, project) == [changed]
    assert save(api, project, branch, title='kept', used=used).json() == made, 'the reply to a key is kept'
    assert saved(api, project, dropped['id'], expect=404).json()['code'] == 'not_found'


@pytest.mark.l5
def test_a_statement_is_forgotten_by_a_restart(private_server):
    api = private_server.session()
    project = new_project(api)
    private_server.restart(kill=True)
    api = private_server.session()
    gone = statement(api, project, NO_STATEMENT, expect=404).json()
    assert gone['code'] == 'not_found' and 'may have committed' in gone['detail']


# ---- L6: parity with the console and the command line ----

@pytest.mark.l6
def test_a_saved_query_is_the_file_the_console_and_command_line_read(api, own, project, branch):
    made = save(api, project, branch, title='shared with the console', sql='select 42').json()
    change(api, project, made['id'], {'sql': 'select 43'})
    runtime = runtime_of(own, 'db_' + branch.removeprefix('br_'))
    native = str(uuid.UUID(made['id'].removeprefix('qry_')))
    path = own.data / 'queries' / runtime / f'{native}.json'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, 'only the owner reads it'
    stored = json.loads(path.read_text())
    assert stored['version'] == 1 and stored['id'] == native and stored['revision'] == 2
    assert stored['title'] == 'shared with the console' and stored['sql'] == 'select 43'
    assert stored['target']['branch'] == str(uuid.UUID(branch.removeprefix('br_')))
    # A change the console makes to the file is what the API then serves.
    stored.update(revision=3, title='renamed in the console')
    path.write_text(json.dumps(stored))
    found = saved(api, project, made['id'])
    assert found.json()['title'] == 'renamed in the console' and found.headers['ETag'] == '"3"'


# ---- With database engines: statements that run ----

@pytest.fixture(scope='module')
def live(engine_api):
    """A project on the engine server whose `main` database is running, as
    (project, database, first branch)."""
    made = engine_api.call('createProject', json={'name': name_for('sql')},
                           headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(engine_api, project, made)
    database = next(d for d in databases(engine_api, project) if d['name'] == 'main')['id']
    return project, database, first(database)


def write(api, project, branch, sql):
    result = done(api, project, run(api, project, branch, sql, read_only=False).json())
    assert result['state'] == 'succeeded', result
    return result


def values(api, project, branch, sql):
    result = done(api, project, run(api, project, branch, sql).json())
    assert result['state'] == 'succeeded', result
    return result['result']['rows']


@pytest.mark.engine
@pytest.mark.l2
def test_a_statement_runs_and_returns_every_value_as_text(engine_api, live):
    api, (project, database, branch) = engine_api, live
    write(api, project, branch, 'create table typed(id bigint primary key, amount numeric(20,4), at timestamptz, note text)')
    inserted = write(api, project, branch,
                     "insert into typed values (1, 12345678901234.5678, '2026-10-11 09:30:00+00', 'é ✓'), (2, null, null, null)")
    assert inserted['result'] == {'columns': [], 'rows': [], 'row_count': 2}

    response = run(api, project, branch, 'select * from typed order by id')
    started = response.json()
    assert STATEMENT_ID.match(started['id']) and response.status_code == 201
    assert response.headers['Location'] == f'/api/v1/projects/{project}/statements/{started["id"]}'
    assert started['state'] in ('running', 'succeeded')
    assert (started['database_id'], started['branch_id']) == (database, branch)
    assert (started['sql'], started['read_only'], started['max_rows'], started['timeout_ms']) == \
        ('select * from typed order by id', True, 200, 10000)
    result = done(api, project, started)
    assert result['state'] == 'succeeded' and result['error'] is None
    assert result['result'] == {
        'columns': [{'name': 'id', 'type': 'int8'}, {'name': 'amount', 'type': 'numeric'},
                    {'name': 'at', 'type': 'timestamptz'}, {'name': 'note', 'type': 'text'}],
        'rows': [['1', '12345678901234.5678', '2026-10-11 09:30:00+00', 'é ✓'], ['2', None, None, None]],
        'row_count': 2,
    }
    assert result['started_at'] == started['started_at'] and result['elapsed_ms'] >= 0
    assert statement(api, project, started['id']).json() == result, 'a finished statement no longer changes'


@pytest.mark.engine
@pytest.mark.l2
def test_a_statement_is_read_only_unless_asked(engine_api, live):
    api, (project, _, branch) = engine_api, live
    write(api, project, branch, 'create table guarded(v int)')
    refused = done(api, project, run(api, project, branch, 'insert into guarded values (1)').json())
    assert refused['state'] == 'failed' and refused['result'] is None
    assert refused['error']['sqlstate'] == '25006', refused
    assert values(api, project, branch, 'select count(*) from guarded') == [['0']]
    write(api, project, branch, 'insert into guarded values (1)')
    assert values(api, project, branch, 'select count(*) from guarded') == [['1']]


@pytest.mark.engine
@pytest.mark.l2
@pytest.mark.parametrize('sql,sqlstate', [('selec 1', '42601'), ('select 1; select 2', '42601'),
                                          ('select * from no_such_table', '42P01'), ('select 1/0', '22012')])
def test_a_failed_statement_reports_postgres_own_code(engine_api, live, sql, sqlstate):
    api, (project, _, branch) = engine_api, live
    failed = done(api, project, run(api, project, branch, sql).json())
    assert failed['state'] == 'failed' and failed['result'] is None
    assert failed['error']['sqlstate'] == sqlstate and failed['error']['message'], failed


@pytest.mark.engine
@pytest.mark.l2
def test_a_result_over_the_limits_fails_and_commits_nothing(engine_api, live):
    api, (project, _, branch) = engine_api, live
    write(api, project, branch, 'create table bounded(v int)')
    over = run(api, project, branch, 'insert into bounded select generate_series(1, 3) returning v',
               read_only=False, max_rows=2).json()
    assert over['max_rows'] == 2
    failed = done(api, project, over)
    assert failed['state'] == 'failed' and failed['error']['sqlstate'] == '54000'
    assert values(api, project, branch, 'select count(*) from bounded') == [['0']], 'the insert was rolled back'
    exact = done(api, project, run(api, project, branch, 'select generate_series(1, 2)', max_rows=2).json())
    assert exact['state'] == 'succeeded' and exact['result']['rows'] == [['1'], ['2']]
    large = done(api, project, run(api, project, branch, "select repeat('x', 300000)").json())
    assert large['state'] == 'failed' and large['error']['sqlstate'] == '54000', 'a result is at most 256 KiB'


@pytest.mark.engine
@pytest.mark.l2
def test_a_statement_that_runs_out_of_time_fails(engine_api, live):
    api, (project, _, branch) = engine_api, live
    slow = done(api, project, run(api, project, branch, 'select pg_sleep(20)', timeout_ms=300).json())
    assert slow['state'] == 'failed' and slow['error']['sqlstate'] == '57014'
    assert slow['elapsed_ms'] < 10000


@pytest.mark.engine
@pytest.mark.l2
def test_cancel_stops_a_running_statement(engine_api, live):
    api, (project, _, branch) = engine_api, live
    write(api, project, branch, 'create table interrupted(v int)')
    started = run(api, project, branch, 'insert into interrupted select 1 from pg_sleep(25)',
                  read_only=False, timeout_ms=30000).json()
    wait(lambda: values(api, project, branch,
                        "select count(*) from pg_stat_activity where query like 'insert into interrupted%'") == [['1']])
    asked = cancel(api, project, started['id']).json()
    assert asked['id'] == started['id'] and asked['state'] in ('cancelling', 'cancelled')
    stopped = done(api, project, started)
    assert stopped['state'] == 'cancelled' and stopped['result'] is None
    assert stopped['error']['sqlstate'] == '57014' and stopped['elapsed_ms'] < 20000
    assert cancel(api, project, started['id']).json() == stopped, 'cancelling a finished statement changes nothing'
    assert values(api, project, branch, 'select count(*) from interrupted') == [['0']]
    finished_one = done(api, project, run(api, project, branch, 'select 1').json())
    assert cancel(api, project, finished_one['id']).json() == finished_one


@pytest.mark.engine
@pytest.mark.l2
def test_a_repeated_key_runs_the_statement_once(engine_api, live):
    api, (project, _, branch) = engine_api, live
    write(api, project, branch, 'create table once(v int)')
    used = key()
    first_reply = run(api, project, branch, 'insert into once values (1)', read_only=False, used=used).json()
    done(api, project, first_reply)
    again = run(api, project, branch, 'insert into once values (1)', read_only=False, used=used).json()
    assert again == first_reply, 'the first reply, not a second run'
    assert values(api, project, branch, 'select count(*) from once') == [['1']]
    reused = run(api, project, branch, 'insert into once values (2)', read_only=False, used=used, expect=409)
    assert reused.json()['code'] == 'idempotency_key_reused'


@pytest.mark.engine
@pytest.mark.l2
def test_the_server_runs_four_statements_at_a_time(engine_api, live):
    api, (project, _, branch) = engine_api, live
    running = [run(api, project, branch, f'select pg_sleep(25), {n}', timeout_ms=30000).json() for n in range(4)]
    used = key()
    full = run(api, project, branch, 'select 1', expect=429, used=used).json()
    assert full['code'] == 'capacity_exhausted'
    for started in running:
        cancel(api, project, started['id'])
    for started in running:
        assert done(api, project, started)['state'] == 'cancelled'
    after = done(api, project, run(api, project, branch, 'select 1', used=used).json())
    assert after['state'] == 'succeeded', 'the refusal was not remembered for the key'


@pytest.mark.engine
@pytest.mark.l2
def test_a_statement_is_read_only_through_its_own_project(engine_api, live):
    api, (project, _, branch) = engine_api, live
    other = api.call('createProject', json={'name': name_for('elsewhere')},
                     headers={'Idempotency-Key': key()}, expect=202).json()['project_id']
    finished_one = done(api, project, run(api, project, branch, 'select 1').json())
    assert statement(api, other, finished_one['id'], expect=404).json()['code'] == 'not_found'
    assert cancel(api, other, finished_one['id'], expect=404).json()['code'] == 'not_found'
    assert run(api, other, branch, 'select 1', expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_running_a_statement_wakes_a_suspended_branch(engine_api, live):
    api, (project, database, branch) = engine_api, live
    write(api, project, branch, 'create table if not exists woken(v int)')
    write(api, project, branch, 'insert into woken values (7)')
    idle(api, project, database)
    finished(api, project, change_database(api, 'suspendDatabase', project, database).json())
    assert read_database(api, project, database).json()['state'] == 'suspended'
    assert values(api, project, branch, 'select v from woken') == [['7']]
    wait(lambda: read_database(api, project, database).json()['state'] == 'running')


# ---- L2: a governed server ----

@pytest.mark.l2
@pytest.mark.parametrize('caller', ['viewer', 'editor', 'administrator'])
def test_a_governed_server_does_not_offer_sql_yet(caller, governed):
    client, project = governed.callers[caller], governed.project
    calls = [('listSavedQueries', {}, None), ('getSavedQuery', {'query': NO_QUERY}, None),
             ('createSavedQuery', {}, {'title': 't', 'sql': 'select 1', 'branch_id': governed.branch}),
             ('runStatement', {}, {'sql': 'select 1', 'branch_id': governed.branch}),
             ('getStatement', {'statement': NO_STATEMENT}, None), ('cancelStatement', {'statement': NO_STATEMENT}, None)]
    for operation, extra, body in calls:
        headers = {'Idempotency-Key': key()} if body else {}
        response = client.call(operation, path={'project': project, **extra}, json=body, headers=headers, expect=403)
        assert response.json()['code'] == 'unavailable_in_profile', operation
