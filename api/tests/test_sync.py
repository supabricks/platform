"""The sync domain: scenarios (L2), durability (L5) and parity (L6).

A pipeline carries a branch's PostgreSQL tables into its analytical copy. A
`snapshot` pipeline can be made, changed, paused and deleted without the
database engines, and a run of it can be queued and cancelled; nothing runs
until the engines and the analytical worker are there. The tests marked
`engine` and `analytics` run real pipelines and read what they publish.

These tests run on a server of their own, like the database tests.
"""
import re
import uuid

import pytest

from harness.server import wait
from test_branches import address, first
from test_databases import (api, create as create_database, delete as delete_database, everything as databases,  # noqa: F401
                            finished, key, name_for, new_project, own, project, psql, read as read_database,
                            runtime_of)

PIPELINE = re.compile(r'^sync_[0-9a-f]{32}$')
RUN = re.compile(r'^run_[0-9a-f]{32}$')
NO_PIPELINE = 'sync_' + '0' * 32
NO_RUN = 'run_' + '0' * 32
LIMITS = {'max_bytes': 1073741824, 'timeout_ms': 300000}
API = 1  # the command line's application API version


def everything(api, project, **params):
    items, cursor = [], None
    while True:
        page = api.call('listSyncPipelines', path={'project': project},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


def runs(api, project, pipeline, **params):
    items, cursor = [], None
    while True:
        page = api.call('listSyncRuns', path={'project': project, 'pipeline': pipeline},
                        params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


def create(api, project, branch, mode='snapshot', expect=201, used=None, **extra):
    return api.call('createSyncPipeline', path={'project': project}, json={'branch_id': branch, 'mode': mode, **extra},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def read(api, project, pipeline, expect=200):
    return api.call('getSyncPipeline', path={'project': project, 'pipeline': pipeline}, expect=expect)


def act(api, operation, project, pipeline, etag='*', expect=200, used=None, **kwargs):
    headers = {'Idempotency-Key': used or key()}
    if etag is not None:
        headers['If-Match'] = etag
    return api.call(operation, path={'project': project, 'pipeline': pipeline}, headers=headers, expect=expect, **kwargs)


def start(api, project, pipeline, expect=201, used=None):
    return api.call('startSyncRun', path={'project': project, 'pipeline': pipeline},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


def cancel(api, project, run, expect=200, used=None):
    return api.call('cancelSyncRun', path={'project': project, 'run': run},
                    headers={'Idempotency-Key': used or key()}, expect=expect)


@pytest.fixture(scope='module')
def shared(api, project):
    """One database for the module: an installation holds 32 branches, and
    without engines a deleted one is not gone."""
    return first(create_database(api, project, name_for('sync')).json()['target']['id'])


@pytest.fixture(scope='module')
def elsewhere(api):
    """Another project, for what must not cross from one to the next."""
    return new_project(api)


@pytest.fixture
def branch(api, project, shared):
    """The module's branch, with no pipeline before the test or after it."""
    def clear():
        for pipeline in everything(api, project, branch_id=shared):
            act(api, 'deleteSyncPipeline', project, pipeline['id'], expect=204)
    clear()
    yield shared
    clear()


# ---- L2: pipelines without engines ----

@pytest.mark.l2
def test_a_snapshot_pipeline_is_created_read_and_listed(api, project, branch):
    assert everything(api, project, branch_id=branch) == []
    used = key()
    made = create(api, project, branch, used=used, schedule={'interval_seconds': 3600})
    pipeline = made.json()
    assert PIPELINE.match(pipeline['id']) and made.headers['ETag'] == '"1"'
    assert made.headers['Location'].endswith(f"/projects/{project}/sync/pipelines/{pipeline['id']}")
    assert pipeline['project_id'] == project and pipeline['branch_id'] == branch
    assert pipeline['database_id'] == 'db_' + branch.removeprefix('br_')
    assert pipeline['mode'] == 'snapshot' and pipeline['state'] == 'active' and pipeline['error'] is None
    assert pipeline['schedule'] == {'interval_seconds': 3600} and pipeline['continuous'] is None
    assert pipeline['limits'] == LIMITS and pipeline['revision'] == 1
    assert pipeline['next_run_at'] > pipeline['created_at'], 'first due one interval from now'
    assert pipeline['last_success_at'] is None and pipeline['last_version_id'] is None
    assert pipeline['capture'] is None and pipeline['freshness'] is None
    assert create(api, project, branch, used=used, schedule={'interval_seconds': 3600}).json() == pipeline, \
        'a repeat is the same pipeline'
    assert create(api, project, branch, used=used, expect=409).json()['code'] == 'idempotency_key_reused'
    found = read(api, project, pipeline['id'])
    assert found.json() == pipeline and found.headers['ETag'] == '"1"'
    assert everything(api, project, branch_id=branch) == [pipeline]
    assert pipeline['id'] in [p['id'] for p in everything(api, project)]
    source = api.call('getSyncSource', path={'project': project, 'branch': branch}, expect=200).json()
    assert source['pipeline_id'] == pipeline['id'] and source['tables_checked'] is False


@pytest.mark.l2
def test_a_branch_has_one_pipeline(api, project, branch):
    create(api, project, branch)
    refused = create(api, project, branch, expect=409).json()
    assert refused['code'] == 'pipeline_exists'
    assert len(everything(api, project, branch_id=branch)) == 1


@pytest.mark.l2
def test_a_pipeline_without_a_schedule_runs_only_when_asked(api, project, branch):
    pipeline = create(api, project, branch, limits={'timeout_ms': 60000}).json()
    assert pipeline['schedule'] is None and pipeline['next_run_at'] is None
    assert pipeline['limits'] == {**LIMITS, 'timeout_ms': 60000}


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'mode': 'snapshot'}, {'branch_id': 7, 'mode': 'snapshot'}, {'mode': 'hourly'}, {'mode': 7},
    {'schedule': {'interval_seconds': 59}}, {'schedule': {'interval_seconds': 2592001}},
    {'schedule': {'interval_seconds': '60'}}, {'schedule': {}}, {'schedule': {'interval_seconds': 60, 'timezone': 'UTC'}},
    {'schedule': 'hourly'}, {'continuous': {'freshness_ms': 5000}}, {'limits': {}}, {'limits': {'max_bytes': 1}},
    {'limits': {'timeout_ms': 1}}, {'limits': {'rows': 5}}, {'tables': ['t']}, {'unknown': 1}])
def test_create_refuses_a_malformed_request(api, project, branch, body):
    sent = body if 'branch_id' in body or body in ({}, {'mode': 'snapshot'}) else {'branch_id': branch, 'mode': 'snapshot', **body}
    refused = api.call('createSyncPipeline', path={'project': project}, json=sent,
                       headers={'Idempotency-Key': key()}, expect=400)
    assert refused.json()['code'] == 'invalid_request'
    assert everything(api, project, branch_id=branch) == []


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {'mode': 'continuous', 'schedule': {'interval_seconds': 60}},
    {'mode': 'continuous', 'continuous': {'freshness_ms': 999}},
    {'mode': 'continuous', 'continuous': {'freshness_ms': 1000, 'batch_interval_ms': 2000}},
    {'mode': 'continuous', 'continuous': {'batch_interval_ms': 199}},
    {'mode': 'triggered', 'continuous': {'freshness_ms': 5000}}])
def test_settings_must_fit_the_mode(api, project, branch, body):
    refused = api.call('createSyncPipeline', path={'project': project}, json={'branch_id': branch, **body},
                       headers={'Idempotency-Key': key()}, expect=400)
    assert refused.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('mode', ['triggered', 'continuous'])
def test_applying_changes_needs_the_engines(api, project, branch, mode):
    refused = create(api, project, branch, mode=mode, expect=503).json()
    assert refused['code'] == 'unavailable'
    assert everything(api, project, branch_id=branch) == []


@pytest.mark.l2
def test_create_requires_an_idempotency_key_and_a_branch_of_the_project(api, project, branch, elsewhere):
    refused = api.call('createSyncPipeline', path={'project': project}, json={'branch_id': branch, 'mode': 'snapshot'},
                       expect=400)
    assert refused.json()['code'] == 'invalid_request'
    for unknown in ['br_' + '0' * 32, 'nonsense', 'db_' + branch.removeprefix('br_')]:
        assert create(api, project, unknown, expect=404).json()['code'] == 'not_found'
    assert create(api, elsewhere, branch, expect=404).json()['code'] == 'not_found', 'a branch of another project'


@pytest.mark.l2
def test_a_pipeline_is_changed_a_field_at_a_time(api, project, branch):
    pipeline = create(api, project, branch, schedule={'interval_seconds': 3600}).json()
    one = {'project': project, 'pipeline': pipeline['id']}
    missing = api.call('updateSyncPipeline', path=one, json={'limits': {'timeout_ms': 20000}},
                       headers={'Idempotency-Key': key()}, expect=428)
    assert missing.json()['code'] == 'precondition_required'
    stale = act(api, 'updateSyncPipeline', project, pipeline['id'], etag='"7"', expect=412,
                json={'limits': {'timeout_ms': 20000}})
    assert stale.json()['code'] == 'revision_mismatch'
    changed = act(api, 'updateSyncPipeline', project, pipeline['id'], etag='"1"', json={'limits': {'timeout_ms': 20000}})
    assert changed.headers['ETag'] == '"2"'
    assert changed.json()['limits'] == {**LIMITS, 'timeout_ms': 20000}
    assert changed.json()['schedule'] == {'interval_seconds': 3600}, 'what was not sent stays'
    same = act(api, 'updateSyncPipeline', project, pipeline['id'], json={'limits': {'timeout_ms': 20000}})
    assert same.headers['ETag'] == '"2"', 'nothing changed, so the revision stays'
    cleared = act(api, 'updateSyncPipeline', project, pipeline['id'], json={'schedule': None}).json()
    assert cleared['schedule'] is None and cleared['next_run_at'] is None and cleared['revision'] == 3
    again = act(api, 'updateSyncPipeline', project, pipeline['id'], json={'schedule': {'interval_seconds': 60}}).json()
    assert again['schedule'] == {'interval_seconds': 60} and again['next_run_at'] is not None
    for body in [{}, {'unknown': 1}, {'branch_id': branch}, {'schedule': {'interval_seconds': 1}}, {'mode': 'weekly'}]:
        refused = act(api, 'updateSyncPipeline', project, pipeline['id'], expect=400, json=body).json()
        assert refused['code'] == 'invalid_request', body
    assert act(api, 'updateSyncPipeline', project, pipeline['id'], expect=503,
               json={'mode': 'triggered', 'schedule': None}).json()['code'] == 'unavailable'
    assert read(api, project, pipeline['id']).json() == again


@pytest.mark.l2
def test_a_pipeline_is_paused_resumed_and_deleted(api, project, branch):
    pipeline = create(api, project, branch, schedule={'interval_seconds': 3600}).json()
    assert act(api, 'pauseSyncPipeline', project, pipeline['id'], etag=None, expect=428).json()['code'] == 'precondition_required'
    assert act(api, 'pauseSyncPipeline', project, pipeline['id'], etag='"9"', expect=412).json()['code'] == 'revision_mismatch'
    used = key()
    paused = act(api, 'pauseSyncPipeline', project, pipeline['id'], etag='"1"', used=used)
    assert paused.json()['state'] == 'paused' and paused.json()['next_run_at'] is None and paused.headers['ETag'] == '"2"'
    assert act(api, 'pauseSyncPipeline', project, pipeline['id'], etag='"1"', used=used).json() == paused.json(), \
        'a repeat is the same reply'
    assert start(api, project, pipeline['id'], expect=409).json()['code'] == 'conflict'
    resumed = act(api, 'resumeSyncPipeline', project, pipeline['id'], etag='"2"').json()
    assert resumed['state'] == 'active' and resumed['next_run_at'] is not None and resumed['revision'] == 3
    act(api, 'deleteSyncPipeline', project, pipeline['id'], etag='"2"', expect=412)
    assert act(api, 'deleteSyncPipeline', project, pipeline['id'], etag='"3"', expect=204).content == b''
    assert read(api, project, pipeline['id'], expect=404).json()['code'] == 'not_found'
    assert everything(api, project, branch_id=branch) == []
    assert act(api, 'deleteSyncPipeline', project, pipeline['id'], expect=404).json()['code'] == 'not_found'
    again = create(api, project, branch).json()
    assert again['id'] != pipeline['id'], 'the branch can be given a new pipeline'


@pytest.mark.l2
def test_a_run_is_queued_read_listed_and_cancelled(api, project, branch):
    """Without engines a run waits; everything about asking for one is real."""
    pipeline = create(api, project, branch).json()
    assert runs(api, project, pipeline['id']) == []
    used = key()
    made = start(api, project, pipeline['id'], used=used)
    run = made.json()
    assert RUN.match(run['id']) and made.headers['Location'].endswith(f"/projects/{project}/sync/runs/{run['id']}")
    assert run['pipeline_id'] == pipeline['id'] and run['branch_id'] == branch
    assert run['trigger'] == 'manual' and run['state'] in ('queued', 'starting') and run['finished_at'] is None
    assert run['version_id'] is None and run['batches'] == 0 and run['error'] is None
    assert start(api, project, pipeline['id'], used=used).json() == run, 'a repeat is the same run'
    busy = start(api, project, pipeline['id'], expect=409).json()
    assert busy['code'] == 'conflict' and 'already in progress' in busy['detail']
    found = api.call('getSyncRun', path={'project': project, 'run': run['id']}, expect=200).json()
    assert found['id'] == run['id']
    assert [r['id'] for r in runs(api, project, pipeline['id'])] == [run['id']]
    cancelled = cancel(api, project, run['id']).json()
    assert cancelled['state'] == 'cancelled' and cancelled['error'] == 'user_cancelled' and cancelled['finished_at']
    assert cancel(api, project, run['id']).json()['state'] == 'cancelled', 'cancelling again changes nothing'
    later = [start(api, project, pipeline['id']).json()['id']]
    cancel(api, project, later[0])
    later.append(start(api, project, pipeline['id']).json()['id'])
    newest_first = [later[1], later[0], run['id']]
    assert [r['id'] for r in runs(api, project, pipeline['id'])] == newest_first
    assert [r['id'] for r in runs(api, project, pipeline['id'], limit=1)] == newest_first
    assert [r['id'] for r in runs(api, project, pipeline['id'], limit=2)] == newest_first
    # Deleting the pipeline cancels what it was doing and keeps the record.
    act(api, 'deleteSyncPipeline', project, pipeline['id'], expect=204)
    ended = api.call('getSyncRun', path={'project': project, 'run': later[1]}, expect=200).json()
    assert ended['state'] == 'cancelled'


@pytest.mark.l2
def test_a_snapshot_pipeline_has_nothing_to_resync(api, project, branch):
    pipeline = create(api, project, branch).json()
    one = {'project': project, 'pipeline': pipeline['id']}
    assert api.call('reviewSyncResync', path=one, expect=409).json()['code'] == 'conflict'
    refused = act(api, 'resyncSyncPipeline', project, pipeline['id'], expect=409, json={'review_hash': 'a' * 64}).json()
    assert refused['code'] == 'conflict'
    for body in [{}, {'review_hash': ''}, {'review_hash': 7}, {'review_hash': 'a' * 129}, {'review_hash': 'a', 'more': 1}]:
        assert act(api, 'resyncSyncPipeline', project, pipeline['id'], expect=400, json=body).json()['code'] == 'invalid_request'
    assert read(api, project, pipeline['id']).json()['revision'] == 1


@pytest.mark.l2
@pytest.mark.parametrize('pipeline', [NO_PIPELINE, 'sync_nothex', 'run_' + '0' * 32, 'x'])
def test_an_unknown_pipeline_is_not_found(api, project, pipeline):
    one = {'project': project, 'pipeline': pipeline}
    responses = [read(api, project, pipeline, expect=404), act(api, 'pauseSyncPipeline', project, pipeline, expect=404),
                 act(api, 'resumeSyncPipeline', project, pipeline, expect=404),
                 act(api, 'deleteSyncPipeline', project, pipeline, expect=404),
                 act(api, 'updateSyncPipeline', project, pipeline, expect=404, json={'schedule': None}),
                 act(api, 'resyncSyncPipeline', project, pipeline, expect=404, json={'review_hash': 'a'}),
                 start(api, project, pipeline, expect=404), api.call('listSyncRuns', path=one, expect=404),
                 api.call('reviewSyncResync', path=one, expect=404)]
    assert {r.json()['code'] for r in responses} == {'not_found'}


@pytest.mark.l2
@pytest.mark.parametrize('run', [NO_RUN, 'run_nothex', 'sync_' + '0' * 32, 'x'])
def test_an_unknown_run_is_not_found(api, project, run):
    assert api.call('getSyncRun', path={'project': project, 'run': run}, expect=404).json()['code'] == 'not_found'
    assert cancel(api, project, run, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
def test_a_pipeline_and_its_runs_belong_to_one_project(api, project, branch, elsewhere):
    pipeline = create(api, project, branch).json()
    run = start(api, project, pipeline['id']).json()
    other = elsewhere
    assert read(api, other, pipeline['id'], expect=404).json()['code'] == 'not_found'
    assert api.call('getSyncRun', path={'project': other, 'run': run['id']}, expect=404).json()['code'] == 'not_found'
    assert cancel(api, other, run['id'], expect=404).json()['code'] == 'not_found'
    assert everything(api, other) == []
    assert api.call('getSyncSource', path={'project': other, 'branch': branch}, expect=404).json()['code'] == 'not_found'
    cancel(api, project, run['id'])


@pytest.mark.l2
def test_listing_pages_and_refuses_what_it_did_not_issue(api, elsewhere):
    project = elsewhere
    made = []
    for database in [d['id'] for d in databases(api, project)] + [
            create_database(api, project, name_for('page')).json()['target']['id'] for _ in range(2)]:
        made.append(create(api, project, first(database)).json()['id'])
    assert len(made) == 3
    assert [p['id'] for p in everything(api, project)] == made, 'oldest first'
    assert [p['id'] for p in everything(api, project, limit=1)] == made
    assert [p['id'] for p in everything(api, project, limit=2)] == made
    for params in [{'cursor': 'zz'}, {'cursor': '5b226e6f225d'}, {'branch_id': 'nonsense'}, {'limit': 0}]:
        refused = api.call('listSyncPipelines', path={'project': project}, params=params, expect=400)
        assert refused.json()['code'] == 'invalid_request', params
    refused = api.call('listSyncRuns', path={'project': project, 'pipeline': made[0]}, params={'cursor': 'zz'}, expect=400)
    assert refused.json()['code'] == 'invalid_request'
    for pipeline in made:
        act(api, 'deleteSyncPipeline', project, pipeline, expect=204)


@pytest.mark.l2
def test_a_source_states_what_sync_needs(api, project, branch):
    source = api.call('getSyncSource', path={'project': project, 'branch': branch}, expect=200).json()
    assert source['branch_id'] == branch and source['pipeline_id'] is None
    assert source['available'] is True and source['tables_checked'] is False
    assert source['modes'] == ['snapshot', 'triggered', 'continuous'] and source['continuous_keeps_compute_awake'] is True
    assert len(source['requirements']) >= 3 and all(isinstance(r, str) for r in source['requirements'])
    for unknown in ['br_' + '0' * 32, 'nonsense']:
        refused = api.call('getSyncSource', path={'project': project, 'branch': unknown}, expect=404)
        assert refused.json()['code'] == 'not_found'


# ---- L5: durability ----

@pytest.mark.l5
def test_pipelines_and_runs_survive_a_killed_daemon(private_server):
    api = private_server.session()
    project = new_project(api)
    database = create_database(api, project, name_for()).json()['target']['id']
    used = key()
    pipeline = create(api, project, first(database), used=used, schedule={'interval_seconds': 86400}).json()
    act(api, 'pauseSyncPipeline', project, pipeline['id'])
    before = read(api, project, pipeline['id']).json()
    private_server.restart(kill=True)
    api = private_server.session()
    assert read(api, project, pipeline['id']).json() == before and before['state'] == 'paused'
    assert create(api, project, first(database), used=used, schedule={'interval_seconds': 86400}).json() == pipeline, \
        'the key still answers with what it made'


# ---- L6: parity with the command line ----

def theirs(server, runtime, **command):
    """One sync command as the command line sends it."""
    return server.rpc(method='api', api_version=API, binding=dict(project_id=runtime, worktree=server.binding['worktree']),
                      action=dict(action='managed_snapshots', command=command))


@pytest.mark.l6
def test_the_api_and_the_command_line_see_the_same_pipelines(own, api):
    # The command line works in the project its checkout is bound to.
    runtime = own.binding['project_id']
    project = next(p['id'] for p in api.call('listProjects', expect=200).json()['items'] if p['name'] == 'api-tests')
    database = create_database(api, project, name_for('cli')).json()['target']['id']
    assert runtime_of(own, database) == runtime
    branch = first(database)
    pipeline = create(api, project, branch, schedule={'interval_seconds': 7200}).json()
    policy_id = str(uuid.UUID(pipeline['id'].removeprefix('sync_')))
    policy = theirs(own, runtime, kind='get', id=policy_id)
    assert policy['state'] == 'active' and policy['revision'] == pipeline['revision']
    assert policy['config']['mode'] == 'snapshot' and policy['config']['schedule']['interval_seconds'] == 7200
    assert policy['branch_id'].replace('-', '') == branch.removeprefix('br_')
    # A change made there is seen here, and its run is one of ours.
    theirs(own, runtime, kind='pause', id=policy_id, expected_revision=1, key=key())
    assert read(api, project, pipeline['id']).json()['state'] == 'paused'
    theirs(own, runtime, kind='resume', id=policy_id, expected_revision=2, key=key())
    made = theirs(own, runtime, kind='run_now', id=policy_id, expected_revision=3, key=key())
    listed = runs(api, project, pipeline['id'])
    assert [r['id'] for r in listed] == ['run_' + made['id'].replace('-', '')]
    cancel(api, project, listed[0]['id'])
    assert theirs(own, runtime, kind='run', id=made['id'])['state'] == 'cancelled'
    # One made there is listed here.
    act(api, 'deleteSyncPipeline', project, pipeline['id'], expect=204)
    again = theirs(own, runtime, kind='create', branch=str(uuid.UUID(branch.removeprefix('br_'))), key=key(),
                   config=dict(mode='snapshot', strategy='full', schedule=None))
    assert [p['id'] for p in everything(api, project, branch_id=branch)] == ['sync_' + again['id'].replace('-', '')]
    act(api, 'deleteSyncPipeline', project, 'sync_' + again['id'].replace('-', ''), expect=204)


# ---- With database engines and the analytical worker: pipelines that run ----

@pytest.fixture(scope='module')
def live(engine_api, request):
    """A project on the engine server with a running database of its own that
    holds one qualifying table, as (project, database, first branch, URI)."""
    made = engine_api.call('createProject', json={'name': name_for('sync')},
                           headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(engine_api, project, made)
    database = finished(engine_api, project, create_database(engine_api, project, name_for('orders')).json())['target']['id']
    uri = address(engine_api, project, database, first(database))
    psql(request, uri, "create table public.orders(id bigint primary key, note text); "
                       "insert into orders select n, 'n' || n from generate_series(1, 50) n")
    return project, database, first(database), uri


def ended(api, project, run, timeout=240):
    def final():
        found = api.call('getSyncRun', path={'project': project, 'run': run}, expect=200).json()
        return found if found['finished_at'] else None
    return wait(final, timeout=timeout)


@pytest.mark.engine
@pytest.mark.analytics
@pytest.mark.l2
def test_a_snapshot_run_publishes_a_version(engine_api, live, request):
    api, (project, _, branch, uri) = engine_api, live
    source = api.call('getSyncSource', path={'project': project, 'branch': branch}, expect=200).json()
    assert source['available'] is True
    pipeline = create(api, project, branch).json()
    try:
        run = start(api, project, pipeline['id']).json()
        done = ended(api, project, run['id'])
        assert done['state'] == 'succeeded', done
        assert done['version_id'] and re.match(r'^[0-9A-F]+/[0-9A-F]+$', done['source_lsn']) and done['error'] is None
        after = read(api, project, pipeline['id']).json()
        assert after['last_version_id'] == done['version_id'] and after['last_success_at'] is not None
        assert after['state'] == 'active' and after['error'] is None
        refused = cancel(api, project, run['id'], expect=409).json()
        assert refused['code'] == 'conflict' and 'published' in refused['detail']
        # A second run publishes a newer version of what the branch holds then.
        psql(request, uri, "insert into orders values (51, 'later')")
        second = ended(api, project, start(api, project, pipeline['id']).json()['id'])
        assert second['state'] == 'succeeded' and second['version_id'] != done['version_id']
        assert [r['id'] for r in runs(api, project, pipeline['id'])] == [second['id'], run['id']]
        assert read(api, project, pipeline['id']).json()['last_version_id'] == second['version_id']
    finally:
        act(api, 'deleteSyncPipeline', project, pipeline['id'], expect=204)
    # The versions it published outlive it, and the branch can have another.
    again = create(api, project, branch).json()
    act(api, 'deleteSyncPipeline', project, again['id'], expect=204)


def gone(api, project, pipeline):
    act(api, 'deleteSyncPipeline', project, pipeline, expect=204)


@pytest.mark.engine
@pytest.mark.analytics
@pytest.mark.l2
def test_a_triggered_pipeline_applies_changes_and_is_resynced_after_review(engine_api, live, request):
    api, (project, _, branch, uri) = engine_api, live
    pipeline = create(api, project, branch, mode='triggered').json()
    one = {'project': project, 'pipeline': pipeline['id']}
    try:
        assert pipeline['mode'] == 'triggered' and pipeline['schedule'] is None and pipeline['continuous'] is None
        # An installation captures changes from one branch at a time.
        other = finished(api, project, create_database(api, project, name_for('second')).json())['target']['id']
        full = create(api, project, first(other), mode='triggered', expect=409).json()
        assert full['code'] == 'capacity_exhausted'
        first_run = ended(api, project, start(api, project, pipeline['id']).json()['id'])
        assert first_run['state'] == 'succeeded', first_run
        seen = read(api, project, pipeline['id']).json()
        assert seen['capture'] is not None and seen['capture']['state'] == 'capturing'
        assert seen['last_version_id'] == first_run['version_id'] and seen['freshness'] is None
        psql(request, uri, "insert into orders values (1001, 'changed'); update orders set note = 'edited' where id = 1")
        second = ended(api, project, start(api, project, pipeline['id']).json()['id'])
        assert second['state'] == 'succeeded' and second['version_id'] != first_run['version_id'], second
        assert second['batches'] >= 1

        review = api.call('reviewSyncResync', path=one, expect=200).json()
        assert review['pipeline_id'] == pipeline['id'] and review['requires_full_copy'] is True
        assert review['deletes_published_versions'] is False and review['revision'] == seen['revision']
        stale = act(api, 'resyncSyncPipeline', project, pipeline['id'], expect=409, json={'review_hash': 'f' * 64}).json()
        assert stale['code'] == 'conflict' and 'Review again' in stale['detail']
        resynced = act(api, 'resyncSyncPipeline', project, pipeline['id'], json={'review_hash': review['review_hash']}).json()
        assert resynced['state'] == 'paused' and resynced['error'] == 'resync_cleanup_pending'
        assert resynced['last_version_id'] == second['version_id'], 'what was published is kept'

        # Refused while the old capture is being cleaned up, then accepted.
        again = wait(lambda: act(api, 'resumeSyncPipeline', project, pipeline['id'], expect=None).status_code == 200
                     and read(api, project, pipeline['id']).json(), timeout=120)
        assert again['state'] == 'active' and again['error'] is None
        third = ended(api, project, start(api, project, pipeline['id']).json()['id'])
        assert third['state'] == 'succeeded', third
    finally:
        gone(api, project, pipeline['id'])


@pytest.mark.engine
@pytest.mark.analytics
@pytest.mark.l2
def test_a_continuous_pipeline_keeps_up_reports_freshness_and_pauses(engine_api, live, request):
    api, (project, _, branch, uri) = engine_api, live

    def free():
        made = api.call('createSyncPipeline', path={'project': project},
                        json={'branch_id': branch, 'mode': 'continuous', 'continuous': {'freshness_ms': 10000}},
                        headers={'Idempotency-Key': key()})
        assert made.status_code in (201, 409), made.text
        return made.json() if made.status_code == 201 else None
    # The capture of the pipeline before this one may still be going away.
    pipeline = wait(free, timeout=120)
    try:
        assert pipeline['continuous'] == {'freshness_ms': 10000, 'batch_interval_ms': 500} and pipeline['schedule'] is None
        refused = start(api, project, pipeline['id'], expect=409).json()
        assert refused['code'] == 'conflict' and 'runs by itself' in refused['detail']

        def state():
            return read(api, project, pipeline['id']).json()
        healthy = wait(lambda: state()['freshness']['state'] == 'healthy' and state(), timeout=240)
        assert healthy['last_version_id'] and healthy['capture']['state'] == 'capturing'
        assert healthy['freshness']['lag_ms'] == 0 and healthy['freshness']['pressure'] is False
        before = healthy['last_version_id']
        psql(request, uri, "insert into orders values (2001, 'streamed')")
        after = wait(lambda: state()['last_version_id'] != before and state()['freshness']['state'] == 'healthy' and state(),
                     timeout=120)
        assert after['capture']['published_lsn'] is not None
        paused = act(api, 'pauseSyncPipeline', project, pipeline['id']).json()
        assert paused['state'] in ('pausing', 'paused')
        settled = wait(lambda: state()['state'] == 'paused' and state(), timeout=120)
        assert settled['freshness']['state'] == 'paused'
    finally:
        gone(api, project, pipeline['id'])
