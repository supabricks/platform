"""L4: each role can do exactly what the specification says, and no more.

Generated from `x-required-role` and `x-profiles`, so an operation is covered
the moment it is served. Every operation is called by someone with no session,
someone with no role anywhere, a viewer, an editor and an administrator of the
project, and an administrator of a different project.

A refusal is decided before the request is read, so these calls send no valid
body: a caller who is allowed gets past authorization and is then answered on
the merits (often 400), and a caller who is not gets 401, 403 or 404.
"""
import pytest

from harness import spec

pytestmark = pytest.mark.l4
SERVED = spec.served()
RANK = {'outsider': 0, 'elsewhere': 0, 'viewer': 1, 'editor': 2, 'administrator': 3}
NEEDS = {'viewer': 1, 'editor': 2, 'administrator': 3}
REFUSED = (401, 403, 404)
# Reads of a resource the governed server cannot hold, because making one
# needs the database engines. A caller who is allowed is told the resource is
# missing, in words a caller refused the project never sees.
ABSENT = {'getBackup': 'no such backup'}


def expected(op, caller):
    """The status and code a refusal must have, or None if the caller is allowed."""
    if caller == 'nobody':
        return 401, 'unauthorized'
    if not op.governed:
        return 403, 'unavailable_in_profile'
    if op.role == 'any':
        return None
    if op.role == 'installation-admin':
        # Service accounts never administer the installation.
        return 403, 'not_permitted'
    if RANK[caller] == 0:
        # A project the caller has no role in is not revealed to exist.
        return 404, 'not_found'
    if RANK[caller] < NEEDS[op.role]:
        return 403, 'not_permitted'
    return None


def send(governed, op, caller, project=None):
    values = dict(project=project or governed.project, operation=governed.operation,
                  database=governed.database, branch=governed.branch,
                  statement='stmt_' + '0' * 32, query='qry_' + '0' * 32, schema='public', table='t', role='reporting', extension='pg_trgm', backup='bkp_' + '0' * 32,
                  source='src_' + '0' * 32, **{'import': 'imp_' + '0' * 32})
    names = [p['name'] for p in op.parameters if p['in'] == 'path']
    return governed.callers[caller].call(op.id, path={name: values[name] for name in names},
                                         json={} if op.method in ('POST', 'PATCH', 'PUT') else None)


@pytest.mark.parametrize('caller', ['nobody', *RANK])
@pytest.mark.parametrize('op', SERVED, ids=repr)
def test_each_caller_is_allowed_or_refused_as_specified(op, caller, governed):
    response = send(governed, op, caller)
    refusal = expected(op, caller)
    if refusal:
        assert (response.status_code, response.json()['code']) == refusal, response.text
    elif op.id in ABSENT:
        # Let through, and then told the thing itself is not there.
        assert response.status_code == 404 and ABSENT[op.id] in response.json()['detail'], response.text
    else:
        assert response.status_code not in REFUSED and response.status_code < 500, response.text


@pytest.mark.parametrize('op', SERVED, ids=repr)
def test_every_served_operation_declares_a_role(op):
    assert op.role in ('any', 'installation-admin', *NEEDS), f'{op.id} has no x-required-role'


@pytest.mark.parametrize('op', [o for o in SERVED if o.role in NEEDS and o.governed], ids=repr)
def test_a_role_in_one_project_gives_nothing_in_another(op, governed):
    response = send(governed, op, 'administrator', project=governed.other_project)
    assert (response.status_code, response.json()['code']) == (404, 'not_found')


@pytest.mark.parametrize('op', [o for o in SERVED if o.role in NEEDS and o.governed], ids=repr)
def test_a_refusal_does_not_depend_on_the_project_existing(op, governed):
    """An unknown project and one the caller cannot see are indistinguishable."""
    unknown = send(governed, op, 'outsider', project='prj_' + '0' * 32)
    hidden = send(governed, op, 'outsider')
    assert unknown.status_code == hidden.status_code == 404
    assert unknown.json() == hidden.json()


def test_a_token_that_is_not_one_is_401(governed):
    for token in ['0' * 64, 'x', 'Bearer']:
        response = governed.server.bearer(token).call('listProjects')
        assert (response.status_code, response.json()['code']) == (401, 'unauthorized')


def test_a_revoked_token_stops_working_at_once(governed):
    principal, caller = governed.server.principal('revoked')
    governed.server.grant(governed.project, principal, 'viewer')
    caller.call('getProject', path=dict(project=governed.project), expect=200)
    governed.server.rpc(method='identity_admin', command=dict(action='revoke', principal=principal))
    assert caller.call('getProject', path=dict(project=governed.project)).status_code == 401


def test_a_disabled_account_stops_working_at_once(governed):
    principal, caller = governed.server.principal('disabled')
    caller.call('listProjects', expect=200)
    governed.server.rpc(method='identity_admin',
                        command=dict(action='disable', principal=principal, disabled=True))
    assert caller.call('listProjects').status_code == 401


def test_removing_a_role_takes_effect_at_once(governed):
    principal, caller = governed.server.principal('leaver')
    governed.server.grant(governed.project, principal, 'editor')
    caller.call('getProject', path=dict(project=governed.project), expect=200)
    governed.server.grant(governed.project, principal, None)
    assert caller.call('getProject', path=dict(project=governed.project)).status_code == 404
    assert caller.call('listProjects', expect=200).json()['items'] == []


def test_a_role_held_through_a_group_counts(governed):
    principal, caller = governed.server.principal('member')
    group = governed.server.rpc(method='identity_admin', command=dict(action='group', label='analysts'))['group_id']
    governed.server.grant(governed.project, group, 'editor', kind='group')
    assert caller.call('getProject', path=dict(project=governed.project)).status_code == 404
    governed.server.rpc(method='identity_admin',
                        command=dict(action='membership', group=group, principal=principal, present=True))
    assert caller.call('getProject', path=dict(project=governed.project), expect=200).json()['my_role'] == 'editor'


def test_every_refusal_is_recorded(governed):
    """Denied calls are written to the security audit, as other governed paths do."""
    before = governed.server.rpc(method='identity_admin', command=dict(action='audit_export', after=0))
    start = before['events'][-1]['sequence'] if before['events'] else 0
    governed.callers['viewer'].call('updateProject', path=dict(project=governed.project), json={}, expect=403)
    events = governed.server.rpc(method='identity_admin', command=dict(action='audit_export', after=start))['events']
    assert any(e['event']['action'] == 'access.denied' and e['event']['actor_id'] == governed.ids['viewer']
               for e in events), events
