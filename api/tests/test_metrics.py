"""The metrics domain: scenarios (L2) and durability (L5).

Metrics are read from what the server has stored, so the operation answers
without the database engines: every point is then a gap. The tests marked
`engine` run on a server that samples every second and check that the points
follow what PostgreSQL is made to do, and stop when the branch does.
"""
import datetime

import pytest

from harness.server import wait
from test_branches import at, first
from test_databases import (api, change, everything as databases, finished, idle, key, name_for, own,  # noqa: F401
                            project, read as read_database)
from test_sql import live, values, write  # noqa: F401

NO_BRANCH = 'br_' + '0' * 32
STEPS = {'1h': (60, 60), '6h': (300, 72), '24h': (900, 96), '7d': (7200, 84)}
VALUES = ['connections_active', 'connections_idle', 'commits_per_second', 'rollbacks_per_second',
          'cache_hit_ratio', 'wal_bytes_per_second', 'storage_bytes']


def metrics(api, place, expect=200, **params):
    return api.call('getBranchMetrics', path=place, params=params, expect=expect)


def moment(text):
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))


@pytest.fixture(scope='module')
def place(api, project):
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    return at(project, database, first(database))


@pytest.mark.l2
@pytest.mark.parametrize('name', list(STEPS))
def test_a_range_is_evenly_spaced_points_ending_now(api, place, name):
    step, count = STEPS[name]
    found = metrics(api, place, range=name).json()
    assert (found['range'], found['step_seconds'], len(found['points'])) == (name, step, count)
    assert (found['sample_interval_seconds'], found['retention_seconds']) == (60, 7 * 24 * 3600)
    times = [moment(p['at']) for p in found['points']]
    assert times[0] == moment(found['from'])
    assert all((b - a).total_seconds() == step for a, b in zip(times, times[1:]))
    assert (moment(found['to']) - times[-1]).total_seconds() == step
    now = datetime.datetime.now(datetime.timezone.utc)
    assert times[-1] <= now < moment(found['to'])


@pytest.mark.l2
def test_a_branch_that_was_never_sampled_is_all_gaps(api, place):
    found = metrics(api, place).json()
    assert found['range'] == '1h', 'an hour unless asked'
    assert all(p[value] is None for p in found['points'] for value in VALUES)


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'range': '2h'}, {'range': ''}, {'range': '1H'}, {'range': '60'}])
def test_a_range_must_be_one_of_the_four(api, place, params):
    assert metrics(api, place, expect=400, **params).json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_metrics_belong_to_a_branch_that_exists(api, place):
    assert metrics(api, {**place, 'branch': NO_BRANCH}, expect=404).json()['code'] == 'not_found'
    assert metrics(api, {**place, 'database': 'db_' + '0' * 32}, expect=404).json()['code'] == 'not_found'


# ---- With database engines, sampled every second ----

def sampled(api, place):
    """The points that hold something, newest last."""
    return [p for p in metrics(api, place).json()['points'] if p['storage_bytes'] is not None]


@pytest.mark.engine
@pytest.mark.l2
def test_points_follow_what_postgres_does(engine_api, live):
    api, (project, database, branch) = engine_api, live
    place = at(project, database, branch)
    assert metrics(api, place).json()['sample_interval_seconds'] == 1
    write(api, project, branch, 'create table if not exists public.ticks (n int)')

    def busy():
        write(api, project, branch, 'insert into public.ticks select generate_series(1, 2000)')
        found = sampled(api, place)
        return found if found and (found[-1]['commits_per_second'] or 0) > 0 \
            and (found[-1]['wal_bytes_per_second'] or 0) > 0 else None
    point = wait(busy, timeout=120)[-1]
    assert point['storage_bytes'] > 1_000_000
    assert point['connections_active'] >= 0 and point['connections_idle'] >= 0
    assert 0 <= point['cache_hit_ratio'] <= 1
    assert point['rollbacks_per_second'] is not None
    # Coarser ranges hold the same samples, in wider steps.
    for name in ('6h', '24h', '7d'):
        wide = metrics(api, place, range=name).json()['points'][-1]
        assert wide['storage_bytes'] is not None and wide['commits_per_second'] is not None


@pytest.mark.engine
@pytest.mark.l2
def test_a_suspended_branch_is_a_gap_and_is_not_woken_by_reading(engine_api):
    api = engine_api
    made = api.call('createProject', json={'name': name_for('quiet')},
                    headers={'Idempotency-Key': key()}, expect=202).json()
    project = made['project_id']
    finished(api, project, made)
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    place = at(project, database, first(database))
    wait(lambda: sampled(api, place), timeout=120)
    idle(api, project, database)
    finished(api, project, change(api, 'suspendDatabase', project, database).json())

    stored = api.call('getBranchMetrics', path=place, expect=200).json()['points']
    last = max(i for i, p in enumerate(stored) if p['storage_bytes'] is not None)
    # Samples are a second apart and a point a minute wide, so wait for a point
    # that starts after the branch stopped.
    def a_gap():
        points = metrics(api, place).json()['points']
        index = next(i for i, p in enumerate(points) if p['at'] == stored[last]['at'])
        later = points[index + 1:]
        return later if len(later) >= 2 else None
    later = wait(a_gap, timeout=200)
    assert all(p[value] is None for p in later[1:] for value in VALUES), 'a gap, not zeros'
    assert read_database(api, project, database).json()['state'] == 'suspended', 'reading metrics wakes nothing'
