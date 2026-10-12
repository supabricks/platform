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


# ---- L2: delete and its impact ----

def delete(api, project, name, etag, expect=202, used=None, **params):
    headers = {'Idempotency-Key': used or key()}
    if etag is not None:
        headers['If-Match'] = etag
    return api.call('deleteProject', path={'project': project}, params={'confirm': name, **params},
                    headers=headers, expect=expect)


def etag_of(api, project):
    return api.call('getProject', path={'project': project}, expect=200).headers['ETag']


@pytest.mark.l2
def test_impact_lists_what_would_go_and_nothing_blocks_a_new_project(api, fresh):
    project, _ = fresh
    from harness.server import wait
    impact = wait(lambda: (lambda body: body if body['removes']['databases'] == 1 else None)(
        api.call('getProjectDeletionImpact', path={'project': project}, expect=200).json()))
    assert impact == {'removes': {'databases': 1, 'branches': 1}, 'blocked_by': []}
    api.call('getProjectDeletionImpact', path={'project': 'prj_' + '0' * 32}, expect=404)


@pytest.mark.l2
def test_delete_hides_the_project_and_frees_its_name_at_once(api, fresh):
    project, name = fresh
    response = delete(api, project, name, etag_of(api, project))
    operation = response.json()
    assert operation['kind'] == 'project.delete'
    assert operation['target'] == {'kind': 'project', 'id': project, 'label': name}
    assert operation['cancellable'] is False
    assert response.headers['Location'].endswith(operation['id'])
    assert api.call('getProject', path={'project': project}, expect=404).json()['code'] == 'not_found'
    assert project not in [p['id'] for p in everything(api)]
    api.call('getProjectDeletionImpact', path={'project': project}, expect=404)
    update(api, project, {'description': 'x'}, expect=404)
    again = create(api, name).json()
    assert again['project_id'] != project, 'the name is free for a new project'


@pytest.mark.l2
def test_the_deletion_can_be_followed_after_the_project_is_gone(api, fresh):
    project, name = fresh
    operation = delete(api, project, name, etag_of(api, project)).json()
    path = {'project': project, 'operation': operation['id']}
    found = api.call('getOperation', path=path, expect=200).json()
    assert found['kind'] == 'project.delete' and found['state'] in ('running', 'succeeded')
    kinds = [o['kind'] for o in api.call('listOperations', path={'project': project}, expect=200).json()['items']]
    assert kinds[0] in ('project.delete', 'branch.force_delete'), kinds
    assert 'project.delete' in kinds
    assert api.call('cancelOperation', path=path, expect=409).json()['code'] == 'not_cancellable'


@pytest.mark.l2
def test_delete_needs_the_current_version_and_the_exact_name(api, fresh):
    project, name = fresh
    etag = etag_of(api, project)
    assert delete(api, project, name, None, expect=428).json()['code'] == 'precondition_required'
    assert delete(api, project, name + 'x', etag, expect=400).json()['code'] == 'invalid_request'
    delete(api, project, name.upper(), etag, expect=400)
    assert api.raw('DELETE', f'/projects/{project}', headers={'If-Match': etag, 'Idempotency-Key': key()}).status_code == 400
    assert api.raw('DELETE', f'/projects/{project}?confirm={name}', headers={'If-Match': etag}).status_code == 400
    update(api, project, {'description': 'changed since the read'})
    assert delete(api, project, name, etag, expect=412).json()['code'] == 'revision_mismatch'
    assert api.call('getProject', path={'project': project}, expect=200).json()['name'] == name, 'still there'
    delete(api, project, name, etag_of(api, project))


@pytest.mark.l2
def test_delete_with_the_same_key_returns_the_first_reply(api, fresh):
    project, name = fresh
    etag, used = etag_of(api, project), key()
    first = delete(api, project, name, etag, used=used)
    again = delete(api, project, name, etag, used=used)
    assert again.json()['id'] == first.json()['id']
    assert delete(api, project, name, etag, expect=404).json()['code'] == 'not_found', 'a new request finds nothing'
    assert api.raw('DELETE', f'/projects/prj_{"0" * 32}?confirm=x',
                   headers={'If-Match': '"1"', 'Idempotency-Key': key()}).status_code == 404


@pytest.mark.l2
def test_a_deleted_project_cannot_be_reattached_from_a_checkout(api, server):
    folder = server.new_source('reattach')
    context, error = server.create_project(folder, 'reattach-me')
    assert error is None
    project = public('prj', context['deployment_id'])
    delete(api, project, 'reattach-me', etag_of(api, project))
    result, error = server.try_rpc(method='project', command=dict(action='attach', deployment=context['deployment_id']),
                                   source=dict(definition_id=context['definition_id'], worktree=str(folder)))
    assert result is None and error is not None
    assert (folder / 'supabricks.toml').exists(), 'a checkout elsewhere on the machine is never removed'


@pytest.mark.l5
def test_a_deletion_survives_a_killed_daemon(private_server):
    api = private_server.session()
    project = create(api, 'going-away').json()['project_id']
    operation = delete(api, project, 'going-away', etag_of(api, project)).json()
    private_server.restart(kill=True)
    api = private_server.session()
    api.call('getProject', path={'project': project}, expect=404)
    assert api.call('getOperation', path={'project': project, 'operation': operation['id']},
                    expect=200).json()['kind'] == 'project.delete'
    create(api, 'going-away')


# ---- With database engines: a delete that finishes ----

@pytest.mark.engine
@pytest.mark.l2
def test_delete_removes_the_databases_and_the_project_folder(engine_api):
    import pathlib
    from harness.server import wait
    api = engine_api
    made = create(api, 'removed-for-real').json()
    project = made['project_id']
    wait(lambda: api.call('getOperation', path={'project': project, 'operation': made['id']}).json()['finished_at'],
         timeout=180)
    record = api.call('getProject', path={'project': project}).json()
    folder = pathlib.Path(record['path'])
    assert folder.is_dir() and record['counts'] == {'databases': 1, 'branches': 1}
    assert api.call('getProjectDeletionImpact', path={'project': project}).json() == {
        'removes': {'databases': 1, 'branches': 1}, 'blocked_by': []}

    operation = delete(api, project, 'removed-for-real', etag_of(api, project)).json()
    path = {'project': project, 'operation': operation['id']}

    def final():
        found = api.call('getOperation', path=path, expect=200).json()
        return found if found['finished_at'] else None
    done = wait(final, timeout=180)
    assert done['state'] == 'succeeded', done
    assert done['result']['project']['id'] == project
    assert not folder.exists(), 'the folder the server made for the project is gone'
    items = api.call('listOperations', path={'project': project}).json()['items']
    removal = next(o for o in items if o['kind'] == 'branch.force_delete')
    assert removal['state'] == 'succeeded' and all(s['state'] == 'succeeded' for s in removal['steps'])
    api.call('getProject', path={'project': project}, expect=404)
    remade = create(api, 'removed-for-real').json()
    wait(lambda: api.call('getOperation', path={'project': remade['project_id'], 'operation': remade['id']})
         .json()['state'] == 'succeeded', timeout=180)


# --- The same operations for signed-in callers on a governed server -----------

@pytest.mark.l2
def test_a_signed_in_caller_lists_only_projects_where_they_hold_a_role(governed):
    def names(caller):
        return [p['name'] for p in governed.callers[caller].call('listProjects', expect=200).json()['items']]
    assert names('viewer') == names('administrator') == ['roles-one']
    assert names('elsewhere') == ['roles-two']
    assert names('outsider') == []
    # The filter and the page size apply to what the caller can see, not to everything.
    page = governed.callers['viewer'].call('listProjects', params=dict(q='roles', limit=1), expect=200).json()
    assert [p['name'] for p in page['items']] == ['roles-one'] and page['next_cursor'] is None


@pytest.mark.l2
@pytest.mark.parametrize('caller', ['viewer', 'editor', 'administrator'])
def test_a_signed_in_caller_sees_their_role_and_no_server_path(caller, governed):
    for project in [governed.callers[caller].call('getProject', path=dict(project=governed.project), expect=200).json(),
                    governed.callers[caller].call('listProjects', expect=200).json()['items'][0]]:
        assert project['my_role'] == caller
        assert 'path' not in project


@pytest.mark.l2
def test_an_administrator_changes_the_description_and_it_is_audited(governed):
    admin, project = governed.callers['administrator'], dict(project=governed.project)
    before = admin.call('getProject', path=project, expect=200)
    changed = admin.call('updateProject', path=project, json=dict(description='Owned by analytics'),
                         headers={'If-Match': before.headers['ETag']}, expect=200).json()
    assert changed['revision'] == before.json()['revision'] + 1
    seen = governed.callers['viewer'].call('getProject', path=project, expect=200).json()
    assert seen['description'] == 'Owned by analytics'
    assert any(e['action'] == 'project.update' and e['actor_id'] == governed.ids['administrator']
               for e in governed.server.audit(governed.project))


@pytest.mark.l2
def test_an_administrator_previews_a_delete_but_a_governed_server_does_not_delete(governed):
    admin, project = governed.callers['administrator'], dict(project=governed.project)
    impact = admin.call('getProjectDeletionImpact', path=project, expect=200).json()
    assert impact['blocked_by'] == []
    current = admin.call('getProject', path=project, expect=200)
    refused = admin.call('deleteProject', path=project, params=dict(confirm='roles-one'),
                         headers={'If-Match': current.headers['ETag'], 'Idempotency-Key': 'governed-delete-1'},
                         expect=403)
    assert refused.json()['code'] == 'unavailable_in_profile'
    admin.call('getProject', path=project, expect=200)


@pytest.mark.l2
def test_the_server_reports_the_governed_profile(governed, api):
    assert governed.callers['outsider'].call('getServerVersion', expect=200).json()['profile'] == 'governed'
    assert api.call('getServerVersion', expect=200).json()['profile'] == 'local'
