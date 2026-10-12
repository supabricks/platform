"""L7: generated requests for every served operation.

Schemathesis builds requests from the specification, including ones the
specification forbids, and checks that the server never answers with a 5xx or
with a response the specification does not declare.
"""
import pytest
import schemathesis
from schemathesis.specs.openapi.checks import ignored_auth

from harness import spec
from harness.client import PREFIX

pytestmark = pytest.mark.l7
schema = schemathesis.openapi.from_dict(spec.bundle())
SERVED = [o.id for o in spec.served()]
schema = schema.include(operation_id=SERVED) if SERVED else schema


@schema.parametrize()
def test_generated_requests(case, api):
    headers = {'Origin': api.base}
    if api.csrf and case.method.upper() != 'GET':
        headers['X-CSRF-Token'] = api.csrf
    # The signed-in cookie stays in the session, so the check that swaps in a
    # bad credential cannot work here. L3 tests missing and forged sessions.
    case.call_and_validate(base_url=api.base + PREFIX, session=api.http, headers=headers,
                           excluded_checks=[ignored_auth])
