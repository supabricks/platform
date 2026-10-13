"""The imports domain: scenarios (L2) and durability (L5).

A file is sent to the server, read by the import worker, and loaded into a new
table. The worker is a Python program with packages of its own, so the tests
that upload or inspect are marked `worker` and are skipped where no Python has
those packages. Loading also needs PostgreSQL: those tests are marked `engine`
as well. What is refused before the worker is needed runs everywhere.

These tests run on a server of their own, like the database tests.
"""
import hashlib
import json
import re
import subprocess
import uuid

import pytest

import harness.server
from harness.server import wait
from test_databases import api, key, name_for, new_project, own, project  # noqa: F401
from test_sql import live, main_of, values, write  # noqa: F401

SOURCE_ID = re.compile(r'^src_[0-9a-f]{32}$')
IMPORT_ID = re.compile(r'^imp_[0-9a-f]{32}$')
NO_SOURCE = 'src_' + '0' * 32
NO_IMPORT = 'imp_' + '0' * 32
NO_BRANCH = 'br_' + '0' * 32
OCTETS = {'Content-Type': 'application/octet-stream'}
FINAL = ('succeeded', 'failed', 'cancelled')
CSV = b'id,email,joined\n1,ada@example.com,2026-01-02\n2,,2026-01-03\n3,cy@example.com,2026-01-04\n'


def declare(api, project, name='people.csv', size=len(CSV), expect=201, used=None, **reading):
    body = {'name': name, 'size_bytes': size, 'format': 'csv', **reading}
    return api.call('createImportSource', path={'project': project}, json=body,
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def send(api, project, source, content, expect=200):
    # A file may take minutes to arrive; the server allows ten.
    return api.call('uploadImportSource', path={'project': project, 'source': source}, data=content,
                    headers=OCTETS, expect=expect, timeout=300)


def source_of(api, project, source, expect=200):
    return api.call('getImportSource', path={'project': project, 'source': source}, expect=expect)


def settled(api, project, source):
    """The source once the server has finished reading it."""
    def read():
        found = source_of(api, project, source).json()
        return found if found['state'] not in ('receiving', 'inspecting') else None
    return wait(read, timeout=60)


def uploaded(api, project, content=CSV, name='people.csv', **reading):
    """A source with its content, after its first inspection."""
    reading.setdefault('format', 'csv')
    made = declare(api, project, name=name, size=len(content), **reading).json()
    send(api, project, made['id'], content)
    return settled(api, project, made['id'])


def remove(api, project, source, expect=204):
    return api.call('deleteImportSource', path={'project': project, 'source': source}, expect=expect)


def sources(api, project, **params):
    found, cursor = [], None
    while True:
        page = api.call('listImportSources', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        found += page['items']
        cursor = page['next_cursor']
        if not cursor:
            return found


def inspect(api, project, source, expect=200, used=None, **reading):
    return api.call('inspectImportSource', path={'project': project, 'source': source}, json=reading,
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def load(api, project, source, branch, table, expect=201, used=None, **extra):
    body = {'source_id': source, 'branch_id': branch, 'table': table, **extra}
    return api.call('createImport', path={'project': project}, json=body,
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def import_of(api, project, id, expect=200):
    return api.call('getImport', path={'project': project, 'import': id}, expect=expect)


def ended(api, project, started, timeout=120):
    def final():
        found = import_of(api, project, started['id']).json()
        return found if found['state'] in FINAL else None
    return wait(final, timeout=timeout)


def imports(api, project, **params):
    found, cursor = [], None
    while True:
        page = api.call('listImports', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        found += page['items']
        cursor = page['next_cursor']
        if not cursor:
            return found


@pytest.fixture
def clean(api, project):
    """Files waiting on the server share one budget; leave none behind."""
    yield
    for source in sources(api, project):
        remove(api, project, source['id'])


# ---- L2: what is refused before any file is read ----

@pytest.mark.l2
def test_a_project_starts_with_no_sources_and_no_imports(api):
    project = new_project(api)
    assert sources(api, project) == []
    assert imports(api, project) == []


@pytest.mark.l2
@pytest.mark.parametrize('operation', ['listImportSources', 'listImports'])
@pytest.mark.parametrize('params', [{'limit': '0'}, {'limit': '201'}, {'limit': 'x'}, {'cursor': 'zz'},
                                    {'cursor': json.dumps(['a']).encode().hex()},
                                    {'cursor': json.dumps(['a', 'b']).encode().hex()}])
def test_lists_reject_bad_paging(api, project, operation, params):
    response = api.call(operation, path={'project': project}, params=params, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('source', [NO_SOURCE, 'people.csv', 'src_1', str(uuid.uuid4()), 'imp_' + '0' * 32])
def test_an_unknown_or_malformed_source_is_404(api, project, source):
    path = {'project': project, 'source': source}
    assert source_of(api, project, source, expect=404).json()['code'] == 'not_found'
    remove(api, project, source, expect=404)
    send(api, project, source, b'a,b\n', expect=404)
    api.call('inspectImportSource', path=path, json={'format': 'csv'}, headers={'Idempotency-Key': key()}, expect=404)


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'name': 'a.csv', 'size_bytes': 4}, {'name': 'a.csv', 'format': 'csv'}, {'size_bytes': 4, 'format': 'csv'},
    {'name': '', 'size_bytes': 4, 'format': 'csv'}, {'name': 'a' * 256, 'size_bytes': 4, 'format': 'csv'},
    {'name': 'a\nb', 'size_bytes': 4, 'format': 'csv'}, {'name': 7, 'size_bytes': 4, 'format': 'csv'},
    {'name': 'a.csv', 'size_bytes': 0, 'format': 'csv'}, {'name': 'a.csv', 'size_bytes': 104857601, 'format': 'csv'},
    {'name': 'a.csv', 'size_bytes': '4', 'format': 'csv'}, {'name': 'a.csv', 'size_bytes': 4.5, 'format': 'csv'},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'tsv'}, {'name': 'a.csv', 'size_bytes': 4, 'format': 'xml'},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'delimiter': ':'},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'delimiter': ',,'},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'header': 'yes'},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'null_strings': 'NA'},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'null_strings': ['x'] * 17},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'null_strings': ['x' * 65]},
    {'name': 'a.json', 'size_bytes': 4, 'format': 'json_lines', 'delimiter': ';'},
    {'name': 'a.json', 'size_bytes': 4, 'format': 'json_array', 'header': False},
    {'name': 'a.pq', 'size_bytes': 4, 'format': 'parquet', 'null_strings': ['NA']},
    {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv', 'path': '/etc/passwd'},
])
def test_declaring_a_source_rejects_an_invalid_body(api, project, body):
    response = api.raw('POST', f'/projects/{project}/import-sources', json=body, headers={'Idempotency-Key': key()})
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'invalid_request'
    assert sources(api, project) == []


@pytest.mark.l2
@pytest.mark.parametrize('headers', [{}, {'Idempotency-Key': 'short'}])
def test_changes_that_start_work_need_an_idempotency_key(api, project, headers):
    for method, path, body in [('POST', 'import-sources', {'name': 'a.csv', 'size_bytes': 4, 'format': 'csv'}),
                               ('POST', 'imports', {'source_id': NO_SOURCE, 'branch_id': main_of(api, project),
                                                    'table': 't'})]:
        response = api.raw(method, f'/projects/{project}/{path}', json=body, headers=headers)
        assert response.status_code in (400, 404), response.text


@pytest.mark.l2
def test_content_must_be_sent_as_octets(api, project):
    for headers in [{'Content-Type': 'application/json'}, {'Content-Type': 'text/csv'}, {}]:
        response = api.raw('PUT', f'/projects/{project}/import-sources/{NO_SOURCE}/content', data=CSV, headers=headers)
        assert response.status_code == 415, response.text
        assert response.json()['code'] == 'unsupported_media_type'


@pytest.mark.l2
@pytest.mark.parametrize('id', [NO_IMPORT, 'people', 'imp_1', str(uuid.uuid4()), 'src_' + '0' * 32])
def test_an_unknown_or_malformed_import_is_404(api, project, id):
    path = {'project': project, 'import': id}
    assert import_of(api, project, id, expect=404).json()['code'] == 'not_found'
    api.call('cancelImport', path=path, expect=404)
    api.call('retryImport', path=path, headers={'Idempotency-Key': key()}, expect=404)


@pytest.mark.l2
@pytest.mark.parametrize('branch', ['main', 'br_1', str(uuid.uuid4())])
def test_listing_imports_rejects_a_branch_that_is_not_an_id(api, project, branch):
    api.call('listImports', path={'project': project}, params={'branch_id': branch}, expect=400)


@pytest.mark.l2
def test_listing_imports_into_an_unknown_branch_is_empty(api, project):
    assert imports(api, project, branch_id=NO_BRANCH) == []


@pytest.mark.l2
@pytest.mark.parametrize('change', [
    {'table': None}, {'table': ''}, {'table': 'a' * 64}, {'table': 7}, {'schema': ''}, {'schema': 'a' * 64},
    {'source_id': None}, {'mode': 'append'}, {'columns': []}, {'columns': 'all'},
    {'columns': [{'input': '0', 'name': 'id', 'type': {'kind': 'bigint'}}]},
    {'columns': [{'input': '0', 'name': 'id', 'type': 'bigint', 'nullable': True}]},
    {'columns': [{'input': '0', 'name': 'id', 'type': {'kind': 'serial'}, 'nullable': True}]},
    {'columns': [{'input': '0', 'name': 'id', 'type': {'kind': 'bigint', 'precision': 3}, 'nullable': True}]},
    {'columns': [{'input': '0', 'name': 'id', 'type': {'kind': 'decimal'}, 'nullable': True}]},
    {'columns': [{'input': '0', 'name': 'id', 'type': {'kind': 'text'}, 'nullable': True, 'default': '1'}]},
])
def test_creating_an_import_rejects_an_invalid_body(api, project, change):
    body = {'source_id': NO_SOURCE, 'branch_id': main_of(api, project), 'table': 't', **change}
    body = {name: value for name, value in body.items() if value is not None}
    response = api.raw('POST', f'/projects/{project}/imports', json=body, headers={'Idempotency-Key': key()})
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_an_import_names_a_branch_and_a_source_of_the_project(api, project):
    assert load(api, project, NO_SOURCE, NO_BRANCH, 't', expect=404).json()['code'] == 'not_found'
    assert load(api, project, NO_SOURCE, main_of(api, project), 't', expect=404).json()['code'] == 'not_found'


# ---- L2: with the import worker: sending and inspecting a file ----

@pytest.mark.worker
@pytest.mark.l2
def test_a_file_is_declared_sent_and_inspected(api, project, clean):
    made = declare(api, project)
    source = made.json()
    assert SOURCE_ID.match(source['id'])
    assert made.headers['Location'].endswith(f"/projects/{project}/import-sources/{source['id']}")
    assert (source['state'], source['size_bytes'], source['received_bytes']) == ('receiving', len(CSV), 0)
    assert source['sha256'] is None and source['inspection'] is None and source['error'] is None
    assert source_of(api, project, source['id']).json() == source

    sent = send(api, project, source['id'], CSV).json()
    assert (sent['state'], sent['received_bytes']) == ('inspecting', len(CSV))

    ready = settled(api, project, source['id'])
    assert (ready['state'], ready['size_bytes'], ready['received_bytes']) == ('ready', len(CSV), len(CSV))
    assert ready['sha256'] == hashlib.sha256(CSV).hexdigest()
    assert ready['error'] is None and ready['expires_at'] == source['expires_at']
    found = ready['inspection']
    assert (found['format'], found['delimiter'], found['header'], found['null_strings']) == ('csv', ',', True, [])
    assert [(c['input'], c['name'], c['type'], c['nullable']) for c in found['columns']] == [
        ('0', 'id', {'kind': 'text'}, True), ('1', 'email', {'kind': 'text'}, True),
        ('2', 'joined', {'kind': 'text'}, True)]
    assert found['sample_rows'][0] == ['1', 'ada@example.com', '2026-01-02']
    assert len(found['sample_rows']) == 3 and found['source_columns'] is None


@pytest.mark.worker
@pytest.mark.l2
def test_declaring_with_the_same_key_returns_the_first_reply(api, project, clean):
    used = key()
    first = declare(api, project, used=used).json()
    assert declare(api, project, used=used).json() == first
    assert [s['id'] for s in sources(api, project)] == [first['id']]
    response = declare(api, project, name='other.csv', used=used, expect=409)
    assert response.json()['code'] == 'idempotency_key_reused'


@pytest.mark.worker
@pytest.mark.l2
@pytest.mark.parametrize('content', [CSV[:-1], CSV + b'4', b''], ids=['short', 'long', 'empty'])
def test_content_of_another_size_is_refused_and_the_source_is_deleted(api, project, content, clean):
    source = declare(api, project).json()['id']
    response = send(api, project, source, content, expect=400)
    assert response.json()['code'] == 'invalid_request'
    source_of(api, project, source, expect=404)
    assert sources(api, project) == []


@pytest.mark.worker
@pytest.mark.l2
def test_a_source_takes_its_content_once(api, project, clean):
    source = uploaded(api, project)
    assert send(api, project, source['id'], CSV, expect=409).json()['code'] == 'conflict'
    assert source_of(api, project, source['id']).json() == source


@pytest.mark.worker
@pytest.mark.l2
def test_a_file_larger_than_one_piece_arrives_whole(api, project, clean):
    content = b'n,text\n' + b''.join(b'%d,%s\n' % (n, b'x' * 90) for n in range(4000))
    source = uploaded(api, project, content, name='large.csv')
    assert (source['state'], source['size_bytes']) == ('ready', len(content))
    assert source['sha256'] == hashlib.sha256(content).hexdigest()
    assert len(source['inspection']['sample_rows']) == 100


@pytest.mark.worker
@pytest.mark.l2
def test_the_reading_given_when_declaring_is_used(api, project, clean):
    content = b'1;NA;x\n2;b;y\n'
    source = uploaded(api, project, content, name='plain.txt', delimiter=';', header=False, null_strings=['NA'])
    found = source['inspection']
    assert (found['delimiter'], found['header'], found['null_strings']) == (';', False, ['NA'])
    assert len(found['columns']) == 3
    assert found['sample_rows'] == [['1', None, 'x'], ['2', 'b', 'y']]


@pytest.mark.worker
@pytest.mark.l2
@pytest.mark.parametrize('format, content, names', [
    ('json_lines', b'{"id": 1, "tags": ["a"]}\n{"id": 2, "name": "b"}\n', ['id', 'tags', 'name']),
    ('json_array', b'[{"id": 1, "name": "a"}, {"id": 2, "name": null}]', ['id', 'name']),
    ('json_document', b'{"anything": [1, 2, {"deep": true}]}', ['document']),
])
def test_json_is_inspected_by_its_keys(api, project, format, content, names, clean):
    source = uploaded(api, project, content, name='data.json', format=format)
    assert source['state'] == 'ready', source
    found = source['inspection']
    assert found['format'] == format
    assert [c['name'] for c in found['columns']] == names
    kinds = {c['name']: c['type']['kind'] for c in found['columns']}
    if format == 'json_lines':
        assert kinds == {'id': 'bigint', 'tags': 'jsonb', 'name': 'text'}
    if format == 'json_document':
        assert kinds == {'document': 'jsonb'}


@pytest.mark.worker
@pytest.mark.l2
def test_parquet_is_inspected_with_its_own_types(api, project, tmp_path, clean):
    file = tmp_path / 'data.parquet'
    made = subprocess.run([str(harness.server.WORKER), '-c', (
        'import sys, decimal, pyarrow as pa, pyarrow.parquet as pq\n'
        'pq.write_table(pa.table({"id": pa.array([1, 2], pa.int32()), "price": pa.array('
        '[decimal.Decimal("1.50"), None], pa.decimal128(9, 2)), "name": ["a", "b"]}), sys.argv[1])\n'), str(file)],
        capture_output=True, text=True, timeout=120)
    assert made.returncode == 0, made.stderr
    source = uploaded(api, project, file.read_bytes(), name='data.parquet', format='parquet')
    assert source['state'] == 'ready', source
    found = source['inspection']
    assert [(c['name'], c['type']) for c in found['columns']] == [
        ('id', {'kind': 'integer'}), ('price', {'kind': 'decimal', 'precision': 9, 'scale': 2}),
        ('name', {'kind': 'text'})]
    assert [(c['name'], c['arrow_type']) for c in found['source_columns']] == [
        ('id', 'int32'), ('price', 'decimal128(9, 2)'), ('name', 'string')]
    assert found['sample_rows'] == [['1', '1.50', 'a'], ['2', None, 'b']]


@pytest.mark.worker
@pytest.mark.l2
def test_a_file_that_is_not_what_it_was_declared_as_fails_and_can_only_be_deleted(api, project, clean):
    source = uploaded(api, project, CSV, name='not.parquet', format='parquet')
    assert source['state'] == 'failed', source
    assert source['error'] and source['inspection'] is None and source['size_bytes'] is None
    assert [s['state'] for s in sources(api, project)] == ['failed']
    assert inspect(api, project, source['id'], format='csv', expect=409).json()['code'] == 'conflict'
    assert load(api, project, source['id'], main_of(api, project), 't', expect=409).json()['code'] == 'conflict'
    remove(api, project, source['id'])
    source_of(api, project, source['id'], expect=404)


@pytest.mark.worker
@pytest.mark.l2
def test_a_ready_source_can_be_inspected_again_another_way(api, project, clean):
    source = uploaded(api, project)
    again = inspect(api, project, source['id'], format='csv', delimiter=';').json()
    assert (again['state'], again['inspection']) == ('inspecting', None)
    reread = settled(api, project, source['id'])
    assert reread['state'] == 'ready' and reread['sha256'] == source['sha256']
    assert [c['name'] for c in reread['inspection']['columns']] == ['id,email,joined']
    assert reread['inspection']['delimiter'] == ';'

    # Read a way it cannot be read: it stays ready, and says why.
    inspect(api, project, source['id'], format='json_array')
    unread = settled(api, project, source['id'])
    assert (unread['state'], unread['inspection']) == ('ready', None)
    assert unread['error']
    assert load(api, project, source['id'], main_of(api, project), 't', expect=409).json()['code'] == 'conflict'

    inspect(api, project, source['id'], format='csv')
    assert settled(api, project, source['id'])['inspection'] == source['inspection']


@pytest.mark.worker
@pytest.mark.l2
@pytest.mark.parametrize('reading', [{}, {'format': 'xml'}, {'format': 'csv', 'delimiter': ':'},
                                     {'format': 'parquet', 'header': False}, {'format': 'csv', 'name': 'x'}])
def test_inspecting_again_rejects_an_invalid_reading(api, project, reading, clean):
    source = uploaded(api, project)
    response = api.raw('POST', f"/projects/{project}/import-sources/{source['id']}/inspect", json=reading,
                       headers={'Idempotency-Key': key()})
    assert response.status_code == 400, response.text
    assert source_of(api, project, source['id']).json() == source


@pytest.mark.worker
@pytest.mark.l2
def test_sources_are_listed_without_inspections_and_page_without_gaps(api, project, clean):
    made = [uploaded(api, project, name=f'{n}.csv')['id'] for n in range(3)]
    listed = sources(api, project)
    assert sorted(s['id'] for s in listed) == sorted(made)
    assert all('inspection' not in s and s['state'] == 'ready' for s in listed)
    assert [s['expires_at'] for s in listed] == sorted((s['expires_at'] for s in listed), reverse=True)
    assert sources(api, project, limit=1) == listed
    assert sources(api, project, limit=2) == listed


@pytest.mark.worker
@pytest.mark.l2
def test_a_deleted_source_is_gone(api, project, clean):
    source = uploaded(api, project)
    remove(api, project, source['id'])
    source_of(api, project, source['id'], expect=404)
    remove(api, project, source['id'], expect=404)
    assert sources(api, project) == []


@pytest.mark.worker
@pytest.mark.l2
def test_a_source_that_is_still_receiving_can_be_deleted(api, project, clean):
    source = declare(api, project).json()['id']
    remove(api, project, source)
    send(api, project, source, CSV, expect=404)


@pytest.mark.worker
@pytest.mark.l2
def test_a_source_belongs_to_one_project_only(api, project, clean):
    source = uploaded(api, project)
    other = new_project(api)
    source_of(api, other, source['id'], expect=404)
    remove(api, other, source['id'], expect=404)
    send(api, other, source['id'], CSV, expect=404)
    assert sources(api, other) == []
    assert load(api, other, source['id'], main_of(api, other), 't', expect=404).json()['code'] == 'not_found'
    assert source_of(api, project, source['id']).json() == source


@pytest.mark.worker
@pytest.mark.l2
def test_an_import_needs_a_source_that_is_ready_and_the_database_engines(api, project, clean):
    branch = main_of(api, project)
    receiving = declare(api, project).json()['id']
    assert load(api, project, receiving, branch, 't', expect=409).json()['code'] == 'conflict'
    remove(api, project, receiving)
    source = uploaded(api, project)
    assert load(api, project, source['id'], branch, 't', expect=503).json()['code'] == 'unavailable'
    assert imports(api, project) == []


# ---- L5: durability ----

@pytest.mark.worker
@pytest.mark.l5
def test_a_source_does_not_outlive_a_restart_of_the_server(private_server):
    server, api = private_server, private_server.session()
    project = api.call('listProjects', expect=200).json()['items'][0]['id']
    ready = uploaded(api, project)
    arriving = declare(api, project, name='late.csv').json()['id']
    server.restart()
    api = server.session()

    # A source is read only under the server that received it.
    kept = source_of(api, project, ready['id']).json()
    assert (kept['state'], kept['error'], kept['inspection']) == ('failed', 'server_restarted', None)
    assert (kept['sha256'], kept['size_bytes']) == (ready['sha256'], None)
    assert inspect(api, project, ready['id'], format='csv', expect=409).json()['code'] == 'conflict'
    remove(api, project, ready['id'])

    # A source still arriving is deleted when the server stops in good order,
    # and is left failed when the server is killed.
    source_of(api, project, arriving, expect=404)
    arriving = declare(api, project, name='late.csv').json()['id']
    server.restart(kill=True)
    api = server.session()
    lost = source_of(api, project, arriving).json()
    assert (lost['state'], lost['error'], lost['size_bytes']) == ('failed', 'interrupted', None)
    send(api, project, arriving, CSV, expect=409)
    remove(api, project, arriving)
    assert sources(api, project) == []


# ---- With database engines: loading ----

COLUMNS = [{'input': '0', 'name': 'id', 'type': {'kind': 'bigint'}, 'nullable': False},
           {'input': '1', 'name': 'email', 'type': {'kind': 'text'}, 'nullable': True},
           {'input': '2', 'name': 'joined', 'type': {'kind': 'date'}, 'nullable': False}]


def table_name():
    return 't_' + uuid.uuid4().hex[:12]


@pytest.fixture
def tidy(engine_api, live):
    yield
    for source in sources(engine_api, live[0]):
        remove(engine_api, live[0], source['id'])


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_a_file_is_loaded_into_a_new_table(engine_api, live, tidy):
    api, (project, database, branch) = engine_api, live
    source = uploaded(api, project, null_strings=[''])
    table = table_name()
    made = load(api, project, source['id'], branch, table, columns=COLUMNS)
    started = made.json()
    assert IMPORT_ID.match(started['id'])
    assert made.headers['Location'].endswith(f"/projects/{project}/imports/{started['id']}")
    assert (started['state'], started['source_id'], started['source_name']) == ('queued', source['id'], 'people.csv')
    assert (started['database_id'], started['branch_id']) == (database, branch)
    assert (started['schema'], started['table'], started['format']) == ('public', table, 'csv')
    assert started['columns'] == COLUMNS and started['committed_rows'] is None and started['error'] is None

    done = ended(api, project, started)
    assert (done['state'], done['committed_rows'], done['error']) == ('succeeded', 3, None)
    assert (done['parsed_rows'], done['copied_rows'], done['attempts'], done['retryable']) == (3, 3, 1, False)
    assert values(api, project, branch, f'select id, email, joined from public."{table}" order by id') == [
        ['1', 'ada@example.com', '2026-01-02'], ['2', None, '2026-01-03'], ['3', 'cy@example.com', '2026-01-04']]
    described = api.call('getTable', path={'project': project, 'database': database, 'branch': branch,
                                           'schema': 'public', 'table': table}, expect=200).json()
    assert [(c['name'], c['type'], c['nullable']) for c in described['columns']] == [
        ('id', 'bigint', False), ('email', 'text', True), ('joined', 'date', False)]
    assert import_of(api, project, started['id']).json() == done
    assert done in imports(api, project) and done in imports(api, project, branch_id=branch)
    # The file has served its purpose, and the server no longer keeps it.
    wait(lambda: source_of(api, project, source['id'], expect=None).status_code == 404)


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_without_columns_the_proposed_ones_are_used_and_the_schema_must_exist(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    source = uploaded(api, project)
    schema, table = 's_' + uuid.uuid4().hex[:8], table_name()
    missing = ended(api, project, load(api, project, source['id'], branch, table, schema=schema).json())
    assert (missing['state'], missing['error'], missing['retryable']) == ('failed', 'postgres_3F000', True)
    write(api, project, branch, f'create schema "{schema}"')
    done = ended(api, project, load(api, project, source['id'], branch, table, schema=schema).json())
    assert (done['state'], done['committed_rows']) == ('succeeded', 3), done
    assert done['columns'] == source['inspection']['columns']
    assert values(api, project, branch, f'select id, email from "{schema}"."{table}" order by id') == [
        ['1', 'ada@example.com'], ['2', ''], ['3', 'cy@example.com']]


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_a_repeated_key_loads_once(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    source = uploaded(api, project)
    table, used = table_name(), key()
    first = load(api, project, source['id'], branch, table, used=used).json()
    assert load(api, project, source['id'], branch, table, used=used).json() == first
    assert ended(api, project, first)['state'] == 'succeeded'
    assert load(api, project, source['id'], branch, table, used=used).json() == first
    assert [i['id'] for i in imports(api, project) if i['table'] == table] == [first['id']]
    assert load(api, project, source['id'], branch, table_name(), used=used, expect=409).json()['code'] \
        == 'idempotency_key_reused'


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_a_table_that_exists_is_left_as_it_was(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    table = table_name()
    write(api, project, branch, f'create table public."{table}" as select 7 as kept')
    source = uploaded(api, project)
    done = ended(api, project, load(api, project, source['id'], branch, table).json())
    assert (done['state'], done['error'], done['retryable'], done['committed_rows']) == (
        'failed', 'target_exists', False, None)
    assert values(api, project, branch, f'select kept from public."{table}"') == [['7']]
    retried = api.call('retryImport', path={'project': project, 'import': done['id']},
                       headers={'Idempotency-Key': key()}, expect=409)
    assert retried.json()['code'] == 'conflict'


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_a_value_that_does_not_fit_fails_the_whole_import_which_can_be_retried(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    source = uploaded(api, project, b'id,email,joined\n1,a,2026-01-02\ntwo,b,2026-01-03\n')
    table = table_name()
    done = ended(api, project, load(api, project, source['id'], branch, table, columns=COLUMNS).json())
    assert (done['state'], done['committed_rows'], done['retryable']) == ('failed', None, True), done
    assert done['error'] == 'invalid_integer'
    assert values(api, project, branch, f"select count(*) from pg_tables where tablename = '{table}'") == [['0']]

    again = api.call('retryImport', path={'project': project, 'import': done['id']},
                     headers={'Idempotency-Key': key()}, expect=200).json()
    assert (again['id'], again['state']) == (done['id'], 'queued')
    second = ended(api, project, again)
    assert (second['state'], second['attempts'], second['error']) == ('failed', 2, 'invalid_integer')

    # Without its file an import cannot be retried, and stays in the history.
    remove(api, project, source['id'])
    kept = import_of(api, project, done['id']).json()
    assert (kept['state'], kept['retryable'], kept['source_id']) == ('failed', False, source['id'])
    api.call('retryImport', path={'project': project, 'import': done['id']},
             headers={'Idempotency-Key': key()}, expect=409)


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_cancelling_a_finished_import_changes_nothing(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    source = uploaded(api, project)
    done = ended(api, project, load(api, project, source['id'], branch, table_name()).json())
    assert done['state'] == 'succeeded'
    assert api.call('cancelImport', path={'project': project, 'import': done['id']}, expect=200).json() == done


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_one_import_runs_at_a_time_and_a_cancelled_one_leaves_no_table(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    content = b'n,text\n' + b''.join(b'%d,%s\n' % (n, b'x' * 200) for n in range(60000))
    large, small = uploaded(api, project, content, name='large.csv'), uploaded(api, project)
    table = table_name()
    first = load(api, project, large['id'], branch, table).json()
    refused = load(api, project, small['id'], branch, table_name(), expect=429)
    assert refused.json()['code'] == 'capacity_exhausted'
    assert remove(api, project, large['id'], expect=409).json()['code'] == 'conflict'

    cancelled = api.call('cancelImport', path={'project': project, 'import': first['id']}, expect=200).json()
    assert cancelled['cancel_requested'] or cancelled['state'] in FINAL
    done = ended(api, project, first)
    # The load may have committed before the cancel reached it; either way the
    # table and the import agree.
    present = values(api, project, branch, f"select count(*) from pg_tables where tablename = '{table}'")
    assert (done['state'], present) in [('cancelled', [['0']]), ('succeeded', [['1']])], done

    after = ended(api, project, load(api, project, small['id'], branch, table_name()).json())
    assert after['state'] == 'succeeded'


@pytest.mark.engine
@pytest.mark.worker
@pytest.mark.l2
def test_imports_are_listed_most_recently_changed_first_and_belong_to_one_project(engine_api, live, tidy):
    api, (project, _, branch) = engine_api, live
    made = [ended(api, project, load(api, project, uploaded(api, project)['id'], branch, table_name()).json())['id']
            for _ in range(3)]
    listed = imports(api, project)
    assert [i['id'] for i in listed][:3] == made[::-1]
    assert [i['updated_at'] for i in listed] == sorted((i['updated_at'] for i in listed), reverse=True)
    assert imports(api, project, limit=1) == listed
    assert imports(api, project, limit=2) == listed

    other = new_project(api)
    assert imports(api, other) == []
    import_of(api, other, made[0], expect=404)
    api.call('cancelImport', path={'project': other, 'import': made[0]}, expect=404)
