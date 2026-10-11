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


# ---- L2: create ----

def key():
    import uuid
    return f'test-{uuid.uuid4()}'


def create(api, name, expect=202, **extra):
    headers = {'Idempotency-Key': extra.pop('key', None) or key()}
    return api.call('createProject', json={'name': name, **extra}, headers=headers, expect=expect)


@pytest.mark.l2
def test_create_returns_an_operation_and_the_project_exists_at_once(api):
    used = key()
    response = create(api, 'Created-One', description='made by a test', key=used)
    operation = response.json()
    assert operation['kind'] == 'project.apply'
    assert operation['idempotency_key'] == used
    assert operation['target']['kind'] == 'project'
    assert operation['target']['id'] == operation['project_id']
    assert response.headers['Location'] == f"/api/v1/projects/{operation['project_id']}/operations/{operation['id']}"
    assert response.headers['Retry-After'] == '1'
    project = api.call('getProject', path={'project': operation['project_id']}, expect=200).json()
    assert project['name'] == 'created-one', 'names are stored in lower case'
    assert project['description'] == 'made by a test'
    from harness.server import wait
    assert wait(lambda: api.call('getProject', path={'project': project['id']}).json()['counts']['databases'] == 1), \
        'a new project gets its main database'
    assert project['id'] in [p['id'] for p in everything(api)]


@pytest.mark.l2
def test_create_with_the_same_key_returns_the_first_reply(api):
    used = key()
    first = create(api, 'replayed', key=used)
    again = create(api, 'replayed', key=used)
    assert again.json() == first.json()
    assert again.headers['Location'] == first.headers['Location']
    assert [p['name'] for p in everything(api)].count('replayed') == 1


@pytest.mark.l2
def test_create_refuses_a_key_reused_for_another_request(api):
    used = key()
    create(api, 'key-owner', key=used)
    response = create(api, 'key-thief', key=used, expect=409)
    assert response.json()['code'] == 'idempotency_key_reused'
    assert 'key-thief' not in [p['name'] for p in everything(api)]


@pytest.mark.l2
def test_create_refuses_a_name_in_use(api):
    create(api, 'taken-name')
    for name in ('taken-name', 'TAKEN-NAME', 'Taken-Name'):
        response = create(api, name, expect=409)
        assert response.json()['code'] == 'name_taken'
    assert [p['name'] for p in everything(api)].count('taken-name') == 1


@pytest.mark.l2
@pytest.mark.parametrize('body', [
    {}, {'name': ''}, {'name': 'has space'}, {'name': '-leading'}, {'name': 'trailing-'}, {'name': 'a' * 41},
    {'name': 'under_score'}, {'name': 7}, {'name': None}, {'name': 'ok', 'description': 'x' * 501},
    {'name': 'ok', 'description': 5}, {'name': 'ok', 'source_id': 'src_' + '0' * 32}, {'name': 'ok', 'extra': 1},
    [], 'text', None,
])
def test_create_rejects_an_invalid_body(api, body):
    response = api.raw('POST', '/projects', json=body, headers={'Idempotency-Key': key()})
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'invalid_request'


@pytest.mark.l2
@pytest.mark.parametrize('headers', [{}, {'Idempotency-Key': 'short'}, {'Idempotency-Key': 'x' * 129},
                                     {'Idempotency-Key': 'has a space in it'}])
def test_create_requires_a_well_formed_idempotency_key(api, headers):
    response = api.raw('POST', '/projects', json={'name': 'needs-a-key'}, headers=headers)
    assert response.status_code == 400
    assert 'needs-a-key' not in [p['name'] for p in everything(api)]


@pytest.mark.l2
def test_an_invalid_create_does_not_use_up_its_key(api):
    used = key()
    create(api, 'not valid!', key=used, expect=400)
    create(api, 'valid-after-all', key=used)


@pytest.mark.l6
def test_a_project_created_here_is_visible_to_the_command_line(api, server):
    operation = create(api, 'seen-by-cli').json()
    project = api.call('getProject', path={'project': operation['project_id']}).json()
    context = server.rpc(method='resolve_binding', source=dict(
        definition_id=server.definition_of(project['path']), worktree=project['path']))
    assert public('prj', context['deployment_id']) == project['id']


@pytest.mark.l5
def test_create_survives_a_killed_daemon_and_its_key_still_replays(private_server):
    api, used = private_server.session(), key()
    first = create(api, 'durable-create', description='kept', key=used).json()
    private_server.restart(kill=True)
    api = private_server.session()
    assert create(api, 'durable-create', description='kept', key=used).json()['id'] == first['id']
    project = api.call('getProject', path={'project': first['project_id']}).json()
    assert (project['name'], project['description']) == ('durable-create', 'kept')
    assert create(api, 'durable-create', expect=409).json()['code'] == 'name_taken'


# ---- L2: rename and description ----

def update(api, project, body, expect=200, **headers):
    return api.call('updateProject', path={'project': project}, json=body, headers=headers, expect=expect)


@pytest.fixture
def fresh(api):
    """A project of its own for one test, as (id, name)."""
    import uuid
    name = f'edit-{uuid.uuid4().hex[:10]}'
    return create(api, name).json()['project_id'], name


@pytest.fixture(scope='module')
def untouched(api):
    """One project shared by tests that must leave it unchanged. The platform
    allows 32 console-created projects, so tests do not each make their own."""
    import uuid
    name = f'same-{uuid.uuid4().hex[:10]}'
    return create(api, name).json()['project_id'], name


@pytest.mark.l2
def test_rename_keeps_the_id_and_raises_the_revision(api, fresh):
    project, _ = fresh
    before = api.call('getProject', path={'project': project})
    response = update(api, project, {'name': 'Renamed-' + project[-6:]})
    after = response.json()
    assert after['name'] == 'renamed-' + project[-6:], 'stored in lower case'
    assert after['id'] == project and after['source_id'] == before.json()['source_id']
    assert after['revision'] == before.json()['revision'] + 1
    assert response.headers['ETag'] == f'"{after["revision"]}"'
    assert api.call('getProject', path={'project': project}).json() == after
    assert after['name'] in [p['name'] for p in everything(api)]


@pytest.mark.l2
def test_description_can_be_set_changed_and_cleared(api, fresh):
    project, name = fresh
    assert update(api, project, {'description': 'first'}).json()['description'] == 'first'
    both = update(api, project, {'description': 'x' * 500, 'name': name}).json()
    assert both['description'] == 'x' * 500 and both['name'] == name
    assert update(api, project, {'description': ''}).json()['description'] == ''


@pytest.mark.l2
def test_a_change_that_changes_nothing_keeps_the_revision(api, untouched):
    project, name = untouched
    before = api.call('getProject', path={'project': project}).json()
    assert update(api, project, {'name': name, 'description': ''}).json() == before
    assert update(api, project, {'name': name.upper()}).json() == before, 'case alone is not a rename'


@pytest.mark.l2
def test_rename_to_a_name_in_use_is_refused(api, fresh):
    project, name = fresh
    other = create(api, name + '-other').json()['project_id']
    for taken in (name, name.upper()):
        assert update(api, other, {'name': taken}, expect=409).json()['code'] == 'name_taken'
    assert api.call('getProject', path={'project': other}).json()['name'] == name + '-other'
    update(api, project, {'name': name + '-moved'})
    assert update(api, other, {'name': name}).json()['name'] == name, 'a freed name can be taken'


@pytest.mark.l2
def test_if_match_makes_a_change_conditional(api, fresh):
    project, _ = fresh
    etag = api.call('getProject', path={'project': project}).headers['ETag']
    first = update(api, project, {'description': 'one'}, **{'If-Match': etag})
    stale = update(api, project, {'description': 'two'}, expect=412, **{'If-Match': etag})
    assert stale.json()['code'] == 'revision_mismatch'
    assert api.call('getProject', path={'project': project}).json()['description'] == 'one'
    update(api, project, {'description': 'two'}, **{'If-Match': first.headers['ETag']})
    update(api, project, {'description': 'three'}, **{'If-Match': '*'})
    for bad in ('nonsense', '"abc"', 'W/"1"'):
        assert update(api, project, {'description': 'x'}, expect=412, **{'If-Match': bad}).json()['code'] == 'revision_mismatch'
    assert api.call('getProject', path={'project': project}).json()['description'] == 'three'


@pytest.mark.l2
@pytest.mark.parametrize('body', [{}, {'name': ''}, {'name': 'has space'}, {'name': 'a' * 41}, {'name': 5},
                                  {'description': 'x' * 501}, {'description': None}, {'id': 'prj_x'},
                                  {'name': 'fine', 'revision': 9}, [], 'text'])
def test_update_rejects_an_invalid_body(api, untouched, body):
    project, name = untouched
    response = api.raw('PATCH', f'/projects/{project}', json=body)
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'invalid_request'
    assert api.call('getProject', path={'project': project}).json()['name'] == name


@pytest.mark.l2
def test_update_of_an_unknown_project_is_404(api):
    response = api.raw('PATCH', '/projects/prj_' + '0' * 32, json={'description': 'x'})
    assert response.status_code == 404
    assert api.raw('PATCH', '/projects/nonsense', json={'description': 'x'}).status_code == 404


@pytest.mark.l2
def test_renaming_one_project_leaves_others_from_the_same_source_alone(api, server):
    source = server.new_source('shared-source')
    for folder, name in [(source, 'shared-prod'), (server.copy_source(source), 'shared-staging')]:
        assert server.create_project(folder, name)[1] is None
    by_name = {p['name']: p for p in everything(api)}
    update(api, by_name['shared-staging']['id'], {'name': 'shared-test'})
    names = {p['name'] for p in everything(api) if p['source_id'] == by_name['shared-prod']['source_id']}
    assert names == {'shared-prod', 'shared-test'}


@pytest.mark.l5
def test_a_rename_survives_a_killed_daemon(private_server):
    api = private_server.session()
    project = create(api, 'before-restart').json()['project_id']
    renamed = update(api, project, {'name': 'after-restart', 'description': 'kept'}).json()
    private_server.restart(kill=True)
    api = private_server.session()
    assert api.call('getProject', path={'project': project}).json() == renamed
    create(api, 'before-restart')
