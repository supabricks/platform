"""L3: rules that hold for every operation the server routes.

Generated from the specification, so a new operation is covered the moment it
is marked implemented. Nothing here is written per domain.
"""
import uuid

import pytest

from harness import spec

pytestmark = pytest.mark.l3
SERVED = spec.served()
PATHS = sorted({o.path for o in SERVED})


def sample(op):
    """A syntactically valid URL for the operation. The resource need not exist."""
    names = [p['name'] for p in op.parameters if p['in'] == 'path']
    return op.url(**{name: str(uuid.uuid4()) for name in names})


def template_url(path):
    while '{' in path:
        start, end = path.index('{'), path.index('}')
        path = path[:start] + str(uuid.uuid4()) + path[end + 1:]
    return path


@pytest.mark.parametrize('op', [o for o in SERVED if o.secured], ids=repr)
def test_without_a_session_is_401(op, anonymous):
    response = anonymous.raw(op.method, sample(op), json={} if op.method != 'GET' else None)
    assert response.status_code == 401
    assert response.json()['code'] == 'unauthorized'


@pytest.mark.parametrize('op', [o for o in SERVED if o.secured], ids=repr)
def test_a_forged_session_is_401(op, server, anonymous):
    cookie = next(iter(server.session().http.cookies)).name
    response = anonymous.raw(op.method, sample(op), headers={'Cookie': f'{cookie}={"0" * 64}'})
    assert response.status_code == 401


@pytest.mark.parametrize('op', SERVED, ids=repr)
def test_another_origin_is_refused(op, api):
    response = api.raw(op.method, sample(op), headers={'Origin': 'https://elsewhere.example'})
    assert response.status_code == 403
    assert response.json()['code'] == 'not_permitted'


@pytest.mark.parametrize('op', [o for o in SERVED if o.method != 'GET'], ids=repr)
def test_a_change_without_the_csrf_token_is_refused(op, api):
    response = api.raw(op.method, sample(op), json={}, csrf=False)
    assert response.status_code == 403
    assert response.json()['code'] == 'not_permitted'


@pytest.mark.parametrize('path', PATHS)
def test_other_methods_are_405_and_name_the_allowed_ones(path, api):
    allowed = {o.method for o in SERVED if o.path == path}
    for method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'} - allowed:
        response = api.raw(method, template_url(path))
        assert response.status_code == 405, method
        assert response.json()['code'] == 'method_not_allowed'
        assert {m.strip() for m in response.headers['Allow'].split(',')} == allowed


@pytest.mark.parametrize('path', PATHS)
def test_a_trailing_slash_or_extra_segment_is_404(path, api):
    for suffix in ('/', '/extra'):
        if any(o.path == path + suffix or o.path.startswith(path + '/{') for o in SERVED):
            continue
        response = api.raw('GET', template_url(path) + suffix)
        assert response.status_code == 404
        assert response.json()['code'] == 'not_found'


@pytest.mark.parametrize('op', [o for o in spec.operations() if not o.served], ids=repr)
def test_an_operation_that_is_not_implemented_is_not_routed(op, api):
    """The server must not answer for anything the status file does not claim."""
    response = api.raw(op.method, sample(op), json={} if op.method != 'GET' else None)
    assert response.status_code in (404, 405)


@pytest.mark.parametrize('op', [o for o in SERVED if o.method == 'GET'], ids=repr)
def test_reads_are_never_cached_and_never_sniffed(op, api):
    response = api.raw(op.method, sample(op))
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


@pytest.mark.parametrize('op', [o for o in SERVED if o.method in ('POST', 'PUT', 'PATCH')], ids=repr)
def test_a_body_that_is_not_json_is_rejected(op, api):
    response = api.raw(op.method, sample(op), data=b'{not json', headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert response.json()['code'] == 'invalid_request'
    response = api.raw(op.method, sample(op), data=b'a=b', headers={'Content-Type': 'application/x-www-form-urlencoded'})
    assert response.status_code == 415


@pytest.mark.parametrize('op', [o for o in SERVED if o.method in ('POST', 'PUT', 'PATCH')], ids=repr)
def test_an_oversized_body_is_413(op, api):
    response = api.raw(op.method, sample(op), data=b'{"x":"' + b'a' * 70000 + b'"}',
                       headers={'Content-Type': 'application/json'})
    assert response.status_code == 413


def test_unknown_paths_are_404(api):
    for path in ('/nothing', '/server', '/server/version/x', '/server/version%2F', '/SERVER/VERSION'):
        response = api.raw('GET', path)
        assert response.status_code == 404, path


def test_the_old_action_endpoint_is_unaffected(api):
    """v1 is mounted beside the existing console API and must not change it."""
    response = api.http.get(api.base + '/api/overview', headers={'x-supabricks-console': '1', 'Origin': api.base}, timeout=10)
    assert response.status_code == 200
    response = api.http.get(api.base + '/api/overview?x=1', headers={'x-supabricks-console': '1'}, timeout=10)
    assert response.status_code == 403, 'query strings stay refused outside /api/v1'
