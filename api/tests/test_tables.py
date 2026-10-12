"""The tables domain: scenarios (L2).

Every operation here reads PostgreSQL, so without database engines the tests
cover what is refused before anything is read: unknown places, and paging,
sort and filter that are not well formed. The tests marked `engine` read real
tables: what a branch holds, how a table is built, and its rows by page, in
order and filtered.

These tests run on a server of their own, like the database tests.
"""
import json
import urllib.parse

import pytest

from test_branches import first
from test_databases import (api, everything as databases, finished, key, name_for, new_project, own, project)  # noqa: F401
from test_sql import cancel, done, run, write

NO_BRANCH = 'br_' + '0' * 32


def place(project, database, branch=None, **more):
    quoted = {name: urllib.parse.quote(value, safe='') for name, value in more.items()}
    return {'project': project, 'database': database, 'branch': branch or first(database), **quoted}


def tables(api, project, database, expect=200, **params):
    return api.call('listTables', path=place(project, database), params=params, expect=expect)


def table(api, project, database, schema, name, expect=200):
    return api.call('getTable', path=place(project, database, schema=schema, table=name), expect=expect)


def rows(api, project, database, schema, name, expect=200, filter=None, **params):
    if filter is not None:
        params['filter'] = filter if isinstance(filter, str) else json.dumps(filter)
    return api.call('listTableRows', path=place(project, database, schema=schema, table=name),
                    params=params, expect=expect)


def every(read, **params):
    """Every item of a listing, following cursors, and how many pages it took."""
    items, cursor, pages = [], None, 0
    while True:
        page = read(**params, **({'cursor': cursor} if cursor else {})).json()
        items += page['items']
        pages += 1
        cursor = page['next_cursor']
        if cursor is None:
            return items, pages


@pytest.fixture(scope='module')
def database(api, project):
    return next(d for d in databases(api, project) if d['name'] == 'main')['id']


# ---- L2: before anything is read ----

@pytest.mark.l2
def test_reading_tables_needs_the_database_engines(api, project, database):
    for response in (tables(api, project, database, expect=503),
                     table(api, project, database, 'public', 'customers', expect=503),
                     rows(api, project, database, 'public', 'customers', expect=503)):
        assert response.json()['code'] == 'unavailable'


@pytest.mark.l2
def test_tables_are_found_only_on_a_branch_that_exists(api, project, database):
    other = new_project(api)
    for path in (place(project, database, NO_BRANCH), place(project, 'db_' + '0' * 32),
                 place(other, database), place('prj_' + '0' * 32, database), place(project, database, 'nonsense')):
        assert api.call('listTables', path=path, expect=404).json()['code'] == 'not_found'
        named = {**path, 'schema': 'public', 'table': 't'}
        assert api.call('getTable', path=named, expect=404).json()['code'] == 'not_found'
        assert api.call('listTableRows', path=named, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('schema,name', [('s' * 64, 't'), ('public', 't' * 64), ('public', '%00'), ('%FF', 't')])
def test_a_name_postgres_could_not_hold_names_nothing(api, project, database, schema, name):
    path = {'project': project, 'database': database, 'branch': first(database), 'schema': schema, 'table': name}
    assert api.call('getTable', path=path, expect=404).json()['code'] == 'not_found'
    assert api.call('listTableRows', path=path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'limit': '0'}, {'limit': '201'}, {'limit': 'x'}, {'cursor': 'zz'},
                                    {'cursor': '5b2261225d'}, {'schema': 'a\x00b'}], ids=repr)
def test_list_rejects_bad_paging(api, project, database, params):
    assert tables(api, project, database, expect=400, **params).json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('params', [
    {'limit': '0'}, {'limit': '201'}, {'cursor': 'zz'}, {'cursor': '5b2261225d'}, {'order': 'down'},
    {'filter': 'email'}, {'filter': '{}'}, {'filter': '[1]'}, {'filter': '[{"column":"a"}]'},
    {'filter': '[{"column":"a","op":"like","value":"x"}]'}, {'filter': '[{"column":"a","op":"eq"}]'},
    {'filter': '[{"column":"a","op":"eq","value":1}]'}, {'filter': '[{"column":"a","op":"eq","value":null}]'},
    {'filter': '[{"column":"a","op":"is_null","value":"x"}]'},
    {'filter': '[{"column":"a","op":"eq","value":"x","and":"more"}]'},
    {'filter': json.dumps([{'column': 'a', 'op': 'is_null'}] * 9)},
    {'filter': json.dumps([{'column': 'a', 'op': 'eq', 'value': 'x' * 257}])},
], ids=repr)
def test_rows_reject_paging_sort_and_filter_that_are_not_well_formed(api, project, database, params):
    response = rows(api, project, database, 'public', 'customers', expect=400, **params)
    assert response.json()['code'] == 'invalid_request'


# ---- With database engines: tables that exist ----

ODD = 'Odd "Name"/x'


@pytest.fixture(scope='module')
def shop(engine_api):
    """A running database with a small schema, as (project, database)."""
    api = engine_api
    made = api.call('createProject', json={'name': name_for('tables')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    for sql in [
        'create schema shop',
        'create table shop.customers(id bigint generated always as identity primary key, email text, '
        "joined timestamptz default now(), balance numeric(12,2) not null default 0, note text)",
        "insert into shop.customers(email, balance, note) select 'user' || n || '@example.com', n * 1.5, "
        "case when n % 2 = 0 then 'even' end from generate_series(1, 25) n",
        "update shop.customers set note = '' where id = 3",
        "update shop.customers set note = repeat('é', 3000) where id = 5",
        'create index customers_email on shop.customers(email)',
        "comment on table shop.customers is 'People who buy'",
        'create table shop.orders(id int primary key, customer bigint references shop.customers, '
        'total numeric check (total >= 0))',
        'create view shop.rich as select id, email from shop.customers where balance > 30',
        'create table public."Odd ""Name""/x"(a text, "b c" int)',
        'insert into public."Odd ""Name""/x" values (\'one\', 1), (\'two\', 2), (\'three\', 3)',
        'create table shop.wide(id int primary key, body text)',
        "insert into shop.wide select n, repeat('x', 1900) from generate_series(1, 200) n",
        'analyze',
    ]:
        write(api, project, first(database), sql)
    return project, database


@pytest.mark.engine
@pytest.mark.l2
def test_list_shows_what_can_be_read_like_a_table(engine_api, shop):
    api, (project, database) = engine_api, shop
    found, _ = every(lambda **p: tables(api, project, database, **p))
    assert [(t['schema'], t['name']) for t in found] == sorted((t['schema'], t['name']) for t in found), \
        'ordered by schema, then name'
    assert not {t['schema'] for t in found} & {'pg_catalog', 'information_schema', '_supabricks'}
    ours = [t for t in found if t['schema'] == 'shop' or t['name'] == ODD]
    assert [(t['schema'], t['name'], t['kind']) for t in ours] == [
        ('public', ODD, 'table'), ('shop', 'customers', 'table'), ('shop', 'orders', 'table'),
        ('shop', 'rich', 'view'), ('shop', 'wide', 'table')]
    customers = ours[1]
    assert customers['row_estimate'] == 25 and customers['size_bytes'] > 0
    assert customers['comment'] == 'People who buy'
    rich = ours[3]
    assert rich['row_estimate'] is None and rich['size_bytes'] is None and rich['comment'] is None
    paged, _ = every(lambda **p: tables(api, project, database, **p), limit=2)
    assert paged == found
    only, pages = every(lambda **p: tables(api, project, database, **p), schema='shop', limit=2)
    assert only == ours[1:] and pages == 2
    assert tables(api, project, database, schema='nothing').json() == {'items': [], 'next_cursor': None}


@pytest.mark.engine
@pytest.mark.l2
def test_a_table_reports_its_columns_keys_indexes_and_constraints(engine_api, shop):
    api, (project, database) = engine_api, shop
    found = table(api, project, database, 'shop', 'customers').json()
    assert (found['schema'], found['name'], found['kind'], found['row_estimate']) == ('shop', 'customers', 'table', 25)
    assert [(c['name'], c['type'], c['nullable']) for c in found['columns']] == [
        ('id', 'bigint', False), ('email', 'text', True), ('joined', 'timestamp with time zone', True),
        ('balance', 'numeric(12,2)', False), ('note', 'text', True)]
    by_name = {c['name']: c for c in found['columns']}
    assert by_name['id']['identity'] is True and by_name['id']['default'] is None
    assert by_name['joined']['default'] == 'now()' and by_name['balance']['default'] == '0'
    assert all(c['generated'] is False for c in found['columns'])
    assert found['primary_key'] == ['id']
    assert {(i['name'], i['unique'], i['primary']) for i in found['indexes']} == {
        ('customers_email', False, False), ('customers_pkey', True, True)}
    assert 'btree (email)' in next(i for i in found['indexes'] if i['name'] == 'customers_email')['definition']
    assert [(c['kind'], c['definition']) for c in found['constraints'] if c['kind'] == 'primary_key'] == [
        ('primary_key', 'PRIMARY KEY (id)')]

    orders = table(api, project, database, 'shop', 'orders').json()
    kinds = {c['kind']: c['definition'] for c in orders['constraints']}
    assert kinds['foreign_key'] == 'FOREIGN KEY (customer) REFERENCES shop.customers(id)'
    assert kinds['check'].startswith('CHECK') and kinds['primary_key'] == 'PRIMARY KEY (id)'

    view = table(api, project, database, 'shop', 'rich').json()
    assert view['kind'] == 'view' and [c['name'] for c in view['columns']] == ['id', 'email']
    assert view['primary_key'] is None and view['indexes'] == [] and view['constraints'] == []

    odd = table(api, project, database, 'public', ODD).json()
    assert odd['name'] == ODD and [c['name'] for c in odd['columns']] == ['a', 'b c'] and odd['primary_key'] is None

    for schema, name in [('shop', 'nothing'), ('nothing', 'customers'), ('pg_catalog', 'pg_class'),
                         ('shop', 'customers_pkey'), ('shop', "customers' or '1'='1")]:
        assert table(api, project, database, schema, name, expect=404).json()['code'] == 'not_found'
        assert rows(api, project, database, schema, name, expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_rows_come_in_key_order_a_page_at_a_time(engine_api, shop):
    api, (project, database) = engine_api, shop
    page = rows(api, project, database, 'shop', 'customers', limit=10).json()
    assert page['columns'] == [{'name': 'id', 'type': 'bigint'}, {'name': 'email', 'type': 'text'},
                               {'name': 'joined', 'type': 'timestamp with time zone'},
                               {'name': 'balance', 'type': 'numeric(12,2)'}, {'name': 'note', 'type': 'text'}]
    assert page['primary_key'] == ['id'] and page['next_cursor']
    assert [r[0] for r in page['items']] == [str(n) for n in range(1, 11)]
    assert page['items'][0][1] == 'user1@example.com' and page['items'][0][3] == '1.50', 'values are text'
    assert page['items'][0][4] is None and page['items'][1][4] == 'even' and page['items'][2][4] == '', \
        'a null and an empty string differ'
    everything, pages = every(lambda **p: rows(api, project, database, 'shop', 'customers', **p), limit=10)
    assert [r[0] for r in everything] == [str(n) for n in range(1, 26)] and pages == 3
    exact, pages = every(lambda **p: rows(api, project, database, 'shop', 'customers', **p), limit=25)
    assert exact == everything and pages == 1, 'a full last page is the last page'


@pytest.mark.engine
@pytest.mark.l2
def test_a_long_value_is_cut_and_said_to_be(engine_api, shop):
    api, (project, database) = engine_api, shop
    page = rows(api, project, database, 'shop', 'customers', filter=[{'column': 'id', 'op': 'eq', 'value': '5'}]).json()
    assert page['truncated'] == [[0, 4]] and page['items'][0][4] == 'é' * 2000
    assert rows(api, project, database, 'shop', 'customers', limit=4).json()['truncated'] == []


@pytest.mark.engine
@pytest.mark.l2
def test_rows_can_be_sorted_by_a_column(engine_api, shop):
    api, (project, database) = engine_api, shop
    def ids(**params):
        return [int(r[0]) for r in every(lambda **p: rows(api, project, database, 'shop', 'customers', **p), **params)[0]]
    assert ids(sort='balance', order='desc', limit=7) == list(range(25, 0, -1)), 'numbers sort as numbers'
    assert ids(sort='id', order='desc')[:3] == [25, 24, 23]
    by_email = ids(sort='email', limit=9)
    assert sorted(by_email) == list(range(1, 26)) and by_email.index(10) < by_email.index(2), 'text sorts as text'
    assert ids(sort='email', order='desc', limit=9) == by_email[::-1]
    by_note = ids(sort='note', limit=6)
    assert sorted(by_note) == list(range(1, 26)), 'ties are broken by the key, so pages neither repeat nor skip'
    assert by_note[0] == 3 and by_note[-11:] == [n for n in range(1, 26) if n % 2 and n not in (3, 5)], \
        'nulls come last ascending'
    assert rows(api, project, database, 'shop', 'customers', sort='nothing', expect=400).json()['code'] == 'invalid_request'


@pytest.mark.engine
@pytest.mark.l2
def test_rows_can_be_filtered(engine_api, shop):
    api, (project, database) = engine_api, shop
    def ids(*conditions, **params):
        read = lambda **p: rows(api, project, database, 'shop', 'customers', filter=list(conditions), **p)
        return [int(r[0]) for r in every(read, **params)[0]]
    def where(column, op, value=None):
        return {'column': column, 'op': op, **({'value': value} if value is not None else {})}
    assert ids(where('id', 'eq', '7')) == [7]
    assert ids(where('id', 'ne', '7'), limit=10) == [n for n in range(1, 26) if n != 7]
    assert ids(where('balance', 'gte', '30')) == list(range(20, 26)), 'a value is read as the column\'s type'
    assert ids(where('balance', 'gt', '30')) == list(range(21, 26))
    assert ids(where('id', 'lt', '3')) == [1, 2] and ids(where('id', 'lte', '3')) == [1, 2, 3]
    assert ids(where('email', 'contains', 'USER1')) == [1, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19], 'ignoring case'
    assert ids(where('email', 'contains', '%')) == [] and ids(where('email', 'contains', '_')) == [], \
        'a pattern character is only itself'
    assert ids(where('note', 'is_null')) == [n for n in range(1, 26) if n % 2 and n not in (3, 5)]
    assert ids(where('note', 'eq', '')) == [3]
    assert ids(where('note', 'not_null'), where('id', 'gt', '20')) == [22, 24], 'every condition must hold'
    assert ids(where('email', 'eq', "x' or '1'='1")) == [], 'a value is never SQL'
    assert ids(where('balance', 'contains', '.50'), limit=5) == [n for n in range(1, 26) if n % 2]
    for bad in (where('id', 'eq', 'seven'), where('nothing', 'is_null'), where('joined', 'gt', 'yesterday-ish')):
        refused = rows(api, project, database, 'shop', 'customers', filter=[bad], expect=400).json()
        assert refused['code'] == 'invalid_request', bad


@pytest.mark.engine
@pytest.mark.l2
def test_a_cursor_continues_only_its_own_listing(engine_api, shop):
    api, (project, database) = engine_api, shop
    whole = rows(api, project, database, 'shop', 'customers', sort='email', limit=25).json()['items']
    cursor = rows(api, project, database, 'shop', 'customers', sort='email', limit=5).json()['next_cursor']
    following = rows(api, project, database, 'shop', 'customers', sort='email', limit=5, cursor=cursor).json()
    assert following['items'] == whole[5:10]
    for other in ({}, {'sort': 'email', 'order': 'desc'}, {'sort': 'id'},
                  {'sort': 'email', 'filter': '[{"column":"id","op":"not_null"}]'}):
        refused = rows(api, project, database, 'shop', 'customers', cursor=cursor, expect=400, **other).json()
        assert refused['code'] == 'invalid_request', other


@pytest.mark.engine
@pytest.mark.l2
def test_views_and_tables_without_a_key_are_read_too(engine_api, shop):
    api, (project, database) = engine_api, shop
    rich = rows(api, project, database, 'shop', 'rich', sort='id', order='desc', limit=3).json()
    assert rich['primary_key'] is None and [r[0] for r in rich['items']] == ['25', '24', '23'] and rich['next_cursor']
    assert len(every(lambda **p: rows(api, project, database, 'shop', 'rich', sort='id', **p), limit=2)[0]) == 5
    odd, pages = every(lambda **p: rows(api, project, database, 'public', ODD, **p), limit=2)
    assert sorted(odd) == [['one', '1'], ['three', '3'], ['two', '2']] and pages == 2
    spaced = rows(api, project, database, 'public', ODD, sort='b c', order='desc',
                  filter=[{'column': 'b c', 'op': 'gte', 'value': '2'}]).json()
    assert spaced['items'] == [['three', '3'], ['two', '2']]
    assert rows(api, project, database, 'shop', 'orders').json()['items'] == []


@pytest.mark.engine
@pytest.mark.l2
def test_a_page_over_the_size_limit_is_refused(engine_api, shop):
    api, (project, database) = engine_api, shop
    refused = rows(api, project, database, 'shop', 'wide', limit=200, expect=422).json()
    assert refused['code'] == 'result_too_large'
    page = rows(api, project, database, 'shop', 'wide', limit=100).json()
    assert len(page['items']) == 100 and page['next_cursor'] and page['truncated'] == []


@pytest.mark.engine
@pytest.mark.l2
def test_a_read_waits_its_turn_for_a_statement_worker(engine_api, shop):
    api, (project, database) = engine_api, shop
    branch = first(database)
    running = [run(api, project, branch, f'select pg_sleep(25), {n}', timeout_ms=30000).json() for n in range(4)]
    try:
        for response in (tables(api, project, database, expect=429),
                         table(api, project, database, 'shop', 'customers', expect=429),
                         rows(api, project, database, 'shop', 'customers', expect=429)):
            assert response.json()['code'] == 'capacity_exhausted'
    finally:
        for started in running:
            cancel(api, project, started['id'])
        for started in running:
            assert done(api, project, started)['state'] == 'cancelled'
    assert len(tables(api, project, database, schema='shop').json()['items']) == 4


# ---- L2: a governed server ----

@pytest.mark.l2
@pytest.mark.parametrize('caller', ['viewer', 'administrator'])
def test_a_governed_server_does_not_offer_tables_yet(caller, governed):
    client = governed.callers[caller]
    base = {'project': governed.project, 'database': governed.database, 'branch': governed.branch}
    for operation, extra in [('listTables', {}), ('getTable', {'schema': 'public', 'table': 't'}),
                             ('listTableRows', {'schema': 'public', 'table': 't'})]:
        response = client.call(operation, path={**base, **extra}, expect=403)
        assert response.json()['code'] == 'unavailable_in_profile', operation
