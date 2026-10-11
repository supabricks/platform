"""The operations domain: scenarios (L2), durability (L5) and parity (L6).

The harness has no database engines, so a project's first apply does not
finish. That is enough to test the view, filters, cancel and durability.
Completion is covered by the engine-backed suites added with databases.
"""
import uuid

import pytest

from harness.server import wait


def key():
    return f'test-{uuid.uuid4()}'


@pytest.fixture(scope='module')
def created(api):
    """A project made through the API, the key that made it, and its operation."""
    used = key()
    operation = api.call('createProject', json={'name': f'ops-{uuid.uuid4().hex[:8]}'},
                         headers={'Idempotency-Key': used}, expect=202).json()
    # The apply starts its database change a moment later, on the daemon's tick.
    wait(lambda: any(o['kind'] == 'database.create' for o in listed(api, operation['project_id'])))
    return operation['project_id'], used, operation


def listed(api, project, **params):
    items, cursor = [], None
    while True:
        page = api.call('listOperations', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


@pytest.mark.l2
def test_get_returns_the_operation_create_started(api, created):
    project, used, operation = created
    response = api.call('getOperation', path={'project': project, 'operation': operation['id']}, expect=200)
    found = response.json()
    assert found['id'] == operation['id']
    assert found['project_id'] == project
    assert found['idempotency_key'] == used
    assert found['state'] in ('pending', 'running')
    assert found['finished_at'] is None
    assert response.headers['Retry-After'] == '1', 'an unfinished operation asks to be polled'
    assert [step['name'] for step in found['steps']] == ['create database.main']


@pytest.mark.l2
def test_list_is_newest_first_and_spans_kinds(api, created):
    project, _, operation = created
    items = listed(api, project)
    assert operation['id'] in [o['id'] for o in items]
    assert {'project.apply', 'database.create'} <= {o['kind'] for o in items}
    assert [o['created_at'] for o in items] == sorted((o['created_at'] for o in items), reverse=True)
    assert all(o['project_id'] == project for o in items)


@pytest.mark.l2
def test_list_pages_without_gaps_or_repeats(api, created):
    project = created[0]
    whole = [o['id'] for o in listed(api, project)]
    assert len(whole) >= 2
    assert [o['id'] for o in listed(api, project, limit=1)] == whole
    page = api.call('listOperations', path={'project': project}, params={'limit': 1}).json()
    assert len(page['items']) == 1 and page['next_cursor']


@pytest.mark.l2
def test_list_filters(api, created):
    project, used, operation = created
    assert [o['id'] for o in listed(api, project, idempotency_key=used)] == [operation['id']]
    assert listed(api, project, idempotency_key='no-such-key') == []
    assert {o['kind'] for o in listed(api, project, kind='project.')} == {'project.apply'}
    assert {o['kind'] for o in listed(api, project, kind='database.create')} == {'database.create'}
    assert listed(api, project, kind='project') == [], 'a prefix needs its trailing dot'
    assert {o['kind'] for o in listed(api, project, target=f'project:{project}')} == {'project.apply'}
    assert listed(api, project, state='succeeded') == []
    unfinished = listed(api, project, state='pending') + listed(api, project, state='running')
    assert {o['id'] for o in unfinished} == {o['id'] for o in listed(api, project) if o['finished_at'] is None}


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'state': 'done'}, {'limit': '0'}, {'cursor': 'zz'}, {'cursor': '5b226e6f225d'}])
def test_list_rejects_bad_parameters(api, created, params):
    response = api.call('listOperations', path={'project': created[0]}, params=params, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_operations_do_not_leak_between_projects(api, created):
    project, _, operation = created
    other = api.call('createProject', json={'name': f'other-{uuid.uuid4().hex[:8]}'},
                     headers={'Idempotency-Key': key()}, expect=202).json()
    assert operation['id'] not in [o['id'] for o in listed(api, other['project_id'])]
    for path in ({'project': other['project_id'], 'operation': operation['id']},
                 {'project': project, 'operation': other['id']}):
        assert api.call('getOperation', path=path, expect=404).json()['code'] == 'not_found'
        assert api.call('cancelOperation', path=path, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('operation', ['op_' + '0' * 32, 'op_short', 'prj_' + '0' * 32, 'x'])
def test_an_unknown_operation_is_404(api, created, operation):
    response = api.raw('GET', f'/projects/{created[0]}/operations/{operation}')
    assert response.status_code == 404
    response = api.raw('GET', f'/projects/prj_{"0" * 32}/operations')
    assert response.status_code == 404


@pytest.mark.l2
def test_cancel_stops_an_unfinished_apply_once(api):
    operation = api.call('createProject', json={'name': f'cancel-{uuid.uuid4().hex[:8]}'},
                         headers={'Idempotency-Key': key()}, expect=202).json()
    path = {'project': operation['project_id'], 'operation': operation['id']}
    assert operation['cancellable'] is True
    accepted = api.call('cancelOperation', path=path, expect=202)
    assert accepted.json()['cancellable'] is False
    assert accepted.headers['Location'].endswith(operation['id'])
    assert api.call('cancelOperation', path=path, expect=409).json()['code'] == 'not_cancellable'

    def finished():
        found = api.call('getOperation', path=path).json()
        return found if found['finished_at'] else None
    final = wait(finished)
    assert final['state'] == 'cancelled'
    assert final['finished_at'] >= final['created_at']
    assert [o['id'] for o in listed(api, operation['project_id'], state='cancelled')] == [operation['id']]


@pytest.mark.l2
def test_a_database_change_cannot_be_cancelled(api, created):
    project = created[0]
    change = next(o for o in listed(api, project) if o['kind'] == 'database.create')
    assert change['cancellable'] is False
    assert change['target']['kind'] == 'branch'
    response = api.call('cancelOperation', path={'project': project, 'operation': change['id']}, expect=409)
    assert response.json()['code'] == 'not_cancellable'


@pytest.mark.l6
def test_the_view_matches_the_daemon_record(api, server, created):
    project, _, _ = created
    change = next(o for o in listed(api, project) if o['kind'] == 'database.create')
    raw = change['id'].removeprefix('op_')
    dashed = f'{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}'
    record = server.rpc(method='operation', id=dashed)
    assert record['status'] == 'pending' and change['state'] in ('pending', 'running')
    assert len(record['steps']) == len(change['steps'])


@pytest.mark.l5
def test_operations_and_their_order_survive_a_killed_daemon(private_server):
    api = private_server.session()
    operation = api.call('createProject', json={'name': 'durable-ops'},
                         headers={'Idempotency-Key': key()}, expect=202).json()
    before = [(o['id'], o['created_at']) for o in listed(api, operation['project_id'])]
    private_server.restart(kill=True)
    api = private_server.session()
    assert [(o['id'], o['created_at']) for o in listed(api, operation['project_id'])] == before
