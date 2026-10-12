"""The extensions domain: scenarios (L2).

Which extensions a branch has is data of its PostgreSQL, so without the
database engines the tests here cover only what is refused before PostgreSQL
is asked. The tests marked `engine` enable and disable real extensions and
check, with SQL, that PostgreSQL has them or not.

These tests run on a server of their own, like the database tests.
"""
import pytest

from test_branches import at, create as create_branch, first
from test_databases import api, everything as databases, finished, key, name_for, own, project  # noqa: F401
from test_sql import done, live, run, values, write  # noqa: F401

NO_BRANCH = 'br_' + '0' * 32
# What the qualified engine ships. A change here is a change to the release.
SHIPPED = ['amcheck', 'neon', 'pageinspect', 'pg_buffercache', 'pg_prewarm', 'pg_trgm', 'pg_visibility', 'plpgsql']


def extensions(api, place, **params):
    found, cursor = [], None
    while True:
        page = api.call('listExtensions', path=place, params={**params, **({'cursor': cursor} if cursor else {})},
                        expect=200).json()
        found += page['items']
        cursor = page['next_cursor']
        if not cursor:
            return found


def enable(api, place, name, expect=200, **kwargs):
    return api.call('enableExtension', path={**place, 'extension': name}, expect=expect, **kwargs)


def disable(api, place, name, expect=204):
    return api.call('disableExtension', path={**place, 'extension': name}, expect=expect)


@pytest.fixture(scope='module')
def place(api, project):
    database = next(d for d in databases(api, project) if d['name'] == 'main')['id']
    return at(project, database, first(database))


# ---- L2: what is refused before PostgreSQL is asked ----

@pytest.mark.l2
def test_extensions_need_the_database_engines(api, place):
    for response in [api.call('listExtensions', path=place, expect=503), enable(api, place, 'pg_trgm', expect=503),
                     disable(api, place, 'pg_trgm', expect=503)]:
        assert response.json()['code'] == 'unavailable'


@pytest.mark.l2
def test_extensions_belong_to_a_branch_that_exists(api, place):
    nowhere = {**place, 'branch': NO_BRANCH}
    for response in [api.call('listExtensions', path=nowhere, expect=404), enable(api, nowhere, 'pg_trgm', expect=404),
                     disable(api, nowhere, 'pg_trgm', expect=404)]:
        assert response.json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('name', ['neon', 'plpgsql'])
def test_an_extension_the_engine_needs_cannot_be_disabled(api, place, name):
    assert disable(api, place, name, expect=409).json()['code'] == 'conflict'


@pytest.mark.l2
@pytest.mark.parametrize('name', ['neon_utils', 'neon_test_utils', 'neon_rmgr'])
def test_the_engines_own_tooling_is_not_offered(api, place, name):
    assert enable(api, place, name, expect=404).json()['code'] == 'not_found'
    assert disable(api, place, name, expect=404).json()['code'] == 'not_found'


@pytest.mark.l2
@pytest.mark.parametrize('body', [{'schema': ''}, {'schema': 'a' * 64}, {'schema': 7}, {'schema': 'pg_catalog'},
                                  {'schema': 'information_schema'}, {'schema': 'neon'}, {'version': '1.5'},
                                  {'cascade': True}])
def test_enabling_rejects_an_invalid_body(api, place, body):
    assert enable(api, place, 'pg_trgm', json=body, expect=400).json()['code'] == 'invalid_request'


# ---- With database engines ----

@pytest.fixture(scope='module')
def lived(live):
    project, database, branch = live
    return at(project, database, branch)


@pytest.mark.engine
@pytest.mark.l2
def test_the_list_is_what_the_engine_ships_and_what_the_branch_has(engine_api, lived):
    api = engine_api
    found = extensions(api, lived)
    assert [e['name'] for e in found] == SHIPPED
    by_name = {e['name']: e for e in found}
    for name in ('neon', 'plpgsql'):
        assert (by_name[name]['required'], by_name[name]['enabled']) == (True, True)
        assert by_name[name]['installed_version'] == by_name[name]['default_version']
    assert by_name['plpgsql']['schema'] == 'pg_catalog'
    assert all(not e['required'] for e in found if e['name'] not in ('neon', 'plpgsql'))
    assert all(e['description'] and e['default_version'] and e['requires'] == [] for e in found)
    assert [e['name'] for e in extensions(api, lived, limit=1)] == SHIPPED
    assert [e['name'] for e in extensions(api, lived, limit=3)] == SHIPPED


@pytest.mark.engine
@pytest.mark.l2
def test_an_extension_is_enabled_used_and_disabled(engine_api, lived):
    api, (project, branch) = engine_api, (lived['project'], lived['branch'])
    disable(api, lived, 'pg_trgm')
    off = next(e for e in extensions(api, lived) if e['name'] == 'pg_trgm')
    assert (off['enabled'], off['installed_version'], off['schema']) == (False, None, None)
    failed = done(api, project, run(api, project, branch, "select similarity('word', 'words')").json())
    assert failed['state'] == 'failed' and failed['error']['sqlstate'] == '42883'

    on = enable(api, lived, 'pg_trgm').json()
    assert (on['enabled'], on['installed_version'], on['schema']) == (True, on['default_version'], 'public')
    assert next(e for e in extensions(api, lived) if e['name'] == 'pg_trgm') == on
    assert values(api, project, branch, "select similarity('word', 'words') > 0.5") == [['t']]
    assert enable(api, lived, 'pg_trgm', json={}).json() == on, 'enabling again changes nothing'

    # Something that uses it keeps it.
    write(api, project, branch, 'create table public.words (w text)')
    write(api, project, branch, 'create index words_trgm on public.words using gin (w gin_trgm_ops)')
    assert disable(api, lived, 'pg_trgm', expect=409).json()['code'] == 'conflict'
    assert values(api, project, branch, "select similarity('word', 'words') > 0.5") == [['t']]
    write(api, project, branch, 'drop table public.words')
    disable(api, lived, 'pg_trgm')
    disable(api, lived, 'pg_trgm')
    assert next(e for e in extensions(api, lived) if e['name'] == 'pg_trgm')['enabled'] is False


@pytest.mark.engine
@pytest.mark.l2
def test_an_extension_can_go_in_a_schema_of_the_projects_own(engine_api, lived):
    api, (project, branch) = engine_api, (lived['project'], lived['branch'])
    disable(api, lived, 'pg_trgm')
    assert enable(api, lived, 'pg_trgm', json={'schema': 'no_such_schema'}, expect=404).json()['code'] == 'not_found'
    write(api, project, branch, 'create schema if not exists tools')
    assert enable(api, lived, 'pg_trgm', json={'schema': 'tools'}).json()['schema'] == 'tools'
    assert values(api, project, branch, "select tools.similarity('word', 'words') > 0.5") == [['t']]
    disable(api, lived, 'pg_trgm')


@pytest.mark.engine
@pytest.mark.l2
def test_an_extension_the_engine_does_not_ship_is_404(engine_api, lived):
    for name in ('vector', 'postgis', 'pgcrypto', 'pg_stat_statements', 'no-such'):
        assert enable(engine_api, lived, name, expect=404).json()['code'] == 'not_found'
        assert disable(engine_api, lived, name, expect=404).json()['code'] == 'not_found'


@pytest.mark.engine
@pytest.mark.l2
def test_an_extension_only_a_superuser_can_install_is_enabled_too(engine_api, lived):
    api = engine_api
    on = enable(api, lived, 'pg_buffercache').json()
    assert (on['enabled'], on['schema']) == (True, 'public')
    disable(api, lived, 'pg_buffercache')


@pytest.mark.engine
@pytest.mark.l2
def test_a_branch_starts_with_its_parents_extensions_and_then_keeps_its_own(engine_api, lived):
    api = engine_api
    project, database = lived['project'], lived['database']
    enable(api, lived, 'pg_prewarm')
    operation = create_branch(api, project, database, name_for('copy')).json()
    child = at(project, database, finished(api, project, operation)['target']['id'])
    assert next(e for e in extensions(api, child) if e['name'] == 'pg_prewarm')['enabled'] is True
    disable(api, child, 'pg_prewarm')
    assert next(e for e in extensions(api, lived) if e['name'] == 'pg_prewarm')['enabled'] is True
    disable(api, lived, 'pg_prewarm')
