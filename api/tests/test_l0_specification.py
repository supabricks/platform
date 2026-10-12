"""L0: the specification is well formed and agrees with the status file.

These need no server.
"""
import pathlib
import subprocess
import sys

import pytest

from harness import spec

pytestmark = pytest.mark.l0
ROLES = {'any', 'viewer', 'editor', 'administrator', 'installation-admin'}
BACKENDS = {'ready', 'cli-only', 'new'}
ALL = spec.operations()


def test_generated_files_are_current():
    script = pathlib.Path(spec.API) / 'scripts/generate.py'
    result = subprocess.run([sys.executable, str(script), '--check'], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_operation_ids_are_unique():
    ids = [o.id for o in ALL]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize('op', ALL, ids=repr)
def test_every_operation_is_traceable(op):
    assert op.body.get('x-backend') in BACKENDS, 'x-backend'
    assert isinstance(op.body.get('x-console-features'), list), 'x-console-features'
    assert op.body.get('summary'), 'summary'


@pytest.mark.parametrize('op', ALL, ids=repr)
def test_every_secured_operation_declares_401(op):
    if op.secured:
        assert '401' in op.body['responses']


@pytest.mark.parametrize('op', spec.served(), ids=repr)
def test_served_operations_are_fully_described(op):
    """What `frozen` requires, checked for everything the server already routes."""
    assert op.body.get('x-required-role') in ROLES, 'x-required-role'
    assert op.body.get('x-backend') == 'ready', 'a served operation is ready by definition'
    assert op.body.get('description'), 'description'
    success = [r for code, r in op.body['responses'].items() if code.startswith('2')]
    assert success, 'a success response'
    for response in success:
        for media in (response.get('content') or {}).values():
            assert 'schema' in media, 'a schema'
            example = media.get('example')
            assert example is not None, 'an example'
            assert not spec.schema_errors(media['schema'], example), 'the example matches its schema'


def test_problem_examples_use_documented_codes():
    """Codes are part of the contract; the shared schema lists the common ones."""
    codes = spec.bundle()['components']['schemas']['Shared.Problem']['properties']['code']['examples']
    assert {'not_found', 'invalid_request', 'not_permitted', 'unauthorized', 'method_not_allowed'} <= set(codes)
