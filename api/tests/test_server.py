"""The server domain: scenarios (L2), durability (L5) and parity with the daemon (L6)."""
import pytest

from harness import spec


@pytest.mark.l2
def test_version_reports_the_build_and_profile(api):
    body = api.call('getServerVersion', expect=200).json()
    assert body['api_version'] == 'v1'
    assert body['profile'] == 'local'
    assert body['version'][0].isdigit()


@pytest.mark.l2
def test_version_ignores_unknown_query_parameters(api):
    assert api.call('getServerVersion', params={'unknown': '1', 'q': 'a b&c'}, expect=200)


@pytest.mark.l2
def test_schema_version_matches_the_daemon(api, server):
    """Parity: REST reports the same state version as the control socket."""
    assert api.call('getServerVersion').json()['schema_version'] == server.rpc(method='status')['schema_version']


@pytest.mark.l5
def test_the_api_returns_after_a_killed_daemon(private_server):
    before = private_server.session().call('getServerVersion', expect=200).json()
    private_server.restart(kill=True)
    assert private_server.session().call('getServerVersion', expect=200).json() == before


@pytest.mark.l1
def test_every_served_operation_has_a_scenario_in_its_domain():
    """Coverage rule: a domain may not be marked complete without tests of its own."""
    import pathlib
    here = pathlib.Path(__file__).parent
    for op in spec.served():
        files = [f for f in here.glob('test_*.py') if not f.name.startswith(('test_l', 'test_fuzz'))]
        assert any(f"'{op.id}'" in f.read_text() for f in files), f'{op.id} has no scenario test'
