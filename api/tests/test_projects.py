"""The projects domain: scenarios (L2), durability (L5) and parity (L6).

A project is one instance of a source. Until createProject is served, these
tests create projects the way the command line does, through the daemon.
"""
import re

import pytest

PUBLIC_ID = re.compile(r'^prj_[0-9a-f]{32}$')


def public(prefix, uuid):
    return f"{prefix}_{uuid.replace('-', '')}"


def everything(api, **params):
    """Every project, following cursors."""
    items, cursor = [], None
    while True:
        page = api.call('listProjects', params={**params, **({'cursor': cursor} if cursor else {})}, expect=200).json()
        items += page['items']
        cursor = page['next_cursor']
        if cursor is None:
            return items


@pytest.fixture(scope='module')
def projects(server):
    """Three projects: two from one source under different names, one from another."""
    sales = server.new_source('sales-analytics')
    made = {}
    for name, folder in [('sales-analytics', sales), ('sales-analytics-staging', server.copy_source(sales)),
                         ('Billing_100%', server.new_source('billing'))]:
        context, error = server.create_project(folder, name if name != 'sales-analytics' else None)
        if name == 'Billing_100%':
            # The name rule for sources and projects is the same, so use a legal one.
            context, error = server.create_project(folder, 'billing-100')
            name = 'billing-100'
        assert error is None, error
        made[name] = context
    return made


# ---- L2: list ----

@pytest.mark.l2
def test_list_returns_every_project_ordered_by_name(api, projects):
    names = [p['name'] for p in everything(api)]
    assert names == sorted(names, key=str.lower)
    assert set(projects) <= set(names)
    assert 'api-tests' in names, 'the project the harness was started with'


@pytest.mark.l2
def test_list_pages_without_gaps_or_repeats(api, projects):
    whole = [p['id'] for p in everything(api)]
    for limit in (1, 2, 3):
        assert [p['id'] for p in everything(api, limit=limit)] == whole
    page = api.call('listProjects', params={'limit': 1}).json()
    assert len(page['items']) == 1 and page['next_cursor']
    assert api.call('listProjects', params={'limit': 200}).json()['next_cursor'] is None


@pytest.mark.l2
def test_list_filters_by_name_ignoring_case(api, projects):
    assert {p['name'] for p in everything(api, q='SALES')} == {'sales-analytics', 'sales-analytics-staging'}
    assert [p['name'] for p in everything(api, q='staging')] == ['sales-analytics-staging']
    assert everything(api, q='no-such-project') == []
    assert len(everything(api, q='')) == len(everything(api))


@pytest.mark.l2
def test_list_filter_treats_wildcards_as_text(api, projects):
    assert everything(api, q='%') == []
    assert everything(api, q='_') == []
    assert everything(api, q="'; DROP TABLE projects; --") == []
    assert len(everything(api)) >= 4


@pytest.mark.l2
@pytest.mark.parametrize('params', [{'limit': '0'}, {'limit': '201'}, {'limit': 'ten'}, {'limit': '-1'},
                                    {'limit': '1.5'}, {'cursor': 'zz'}, {'cursor': '5b5d'}, {'cursor': '7b7d'}])
def test_list_rejects_bad_paging(api, params):
    response = api.call('listProjects', params=params, expect=400)
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
def test_list_rejects_a_repeated_parameter(api):
    response = api.raw('GET', '/projects?limit=1&limit=2')
    assert response.status_code == 400


# ---- L2: get ----

@pytest.mark.l2
def test_get_returns_the_project_and_its_revision_as_etag(api, projects):
    listed = next(p for p in everything(api) if p['name'] == 'sales-analytics-staging')
    response = api.call('getProject', path={'project': listed['id']}, expect=200)
    project = response.json()
    assert project == listed
    assert PUBLIC_ID.match(project['id'])
    assert response.headers['ETag'] == f'"{project["revision"]}"'
    assert project['counts'] == {'databases': 0, 'branches': 0}
    assert project['my_role'] is None, 'the local owner holds no role'
    assert project['description'] == ''
    assert project['created_at'].endswith('Z')


@pytest.mark.l2
def test_get_shows_the_folder_to_the_local_owner(api, projects):
    listed = next(p for p in everything(api) if p['name'] == 'billing-100')
    assert listed['path'].startswith('/')


@pytest.mark.l2
@pytest.mark.parametrize('project', ['prj_' + '0' * 32, 'prj_short', 'db_' + '0' * 32, 'x', 'PRJ_' + 'A' * 32,
                                     '5f0c2a9e-7b1d-4c3f-a8e6-d2b4c1a09f73', '%27%20OR%201=1'])
def test_get_of_an_unknown_or_malformed_id_is_404(api, project):
    response = api.raw('GET', f'/projects/{project}')
    assert response.status_code == 404
    assert response.json()['code'] == 'not_found'


# ---- L2: one source, several projects ----

@pytest.mark.l2
def test_two_projects_from_one_source_are_separate(api, projects):
    by_name = {p['name']: p for p in everything(api)}
    production, staging = by_name['sales-analytics'], by_name['sales-analytics-staging']
    assert production['source_id'] == staging['source_id']
    assert production['id'] != staging['id']
    assert by_name['billing-100']['source_id'] != production['source_id']


@pytest.mark.l2
def test_a_name_in_use_is_refused_even_for_the_same_source(server, projects):
    again = server.copy_source(server.root / 'sources' / next(iter(
        p.name for p in (server.root / 'sources').iterdir()
        if 'name="sales-analytics"' in (p / 'supabricks.toml').read_text())))
    for name in (None, 'sales-analytics', 'SALES-ANALYTICS', 'sales-analytics-staging'):
        context, error = server.create_project(again, name)
        assert context is None, name
        assert 'name_taken' in str(error), error
    context, error = server.create_project(again, 'sales-analytics-test')
    assert error is None, error


@pytest.mark.l2
def test_an_invalid_name_is_refused(server):
    for name in ('', 'has space', 'a' * 200, '../x'):
        context, error = server.create_project(server.new_source('valid'), name)
        assert context is None, name


# ---- L6: parity with the command line ----

@pytest.mark.l6
def test_ids_match_what_the_command_line_reports(api, projects):
    by_name = {p['name']: p for p in everything(api)}
    for name, context in projects.items():
        assert by_name[name]['id'] == public('prj', context['deployment_id'])
        assert by_name[name]['source_id'] == public('src', context['definition_id'])


# ---- L5: durability ----

@pytest.mark.l5
def test_projects_survive_a_killed_daemon(private_server):
    source = private_server.new_source('durable')
    for folder, name in [(source, 'durable'), (private_server.copy_source(source), 'durable-staging')]:
        assert private_server.create_project(folder, name)[1] is None
    before = everything(private_server.session())
    private_server.restart(kill=True)
    assert everything(private_server.session()) == before
    assert private_server.create_project(private_server.copy_source(source), 'DURABLE')[0] is None, \
        'the name rule holds after a restart'
