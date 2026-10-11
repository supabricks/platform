"""L7: generated requests for every served operation.

Schemathesis builds requests from the specification, including ones the
specification forbids, and checks that the server never answers with a 5xx or
with a response the specification does not declare.
"""
import pytest
import schemathesis
from schemathesis.specs.openapi.checks import ignored_auth, positive_data_acceptance

from harness import spec
from harness.client import PREFIX

pytestmark = pytest.mark.l7
# Only what the server routes today: a method the specification describes but
# the server does not serve yet is, correctly, missing from `Allow`.
schema = schemathesis.openapi.from_dict(spec.only(lambda o: o.served))


@schema.parametrize()
def test_generated_requests(case, api):
    headers = {'Origin': api.base}
    if api.csrf and case.method.upper() != 'GET':
        headers['X-CSRF-Token'] = api.csrf
    # The signed-in cookie stays in the session, so the check that swaps in a
    # bad credential cannot work here. L3 tests missing and forged sessions.
    excluded = [ignored_auth]
    # A cursor is valid only if this server issued it, which a schema cannot
    # say. A made-up one is correctly refused, so it is not "valid data".
    if (case.query or {}).get('cursor'):
        excluded.append(positive_data_acceptance)
    case.call_and_validate(base_url=api.base + PREFIX, session=api.http, headers=headers,
                           excluded_checks=excluded)
