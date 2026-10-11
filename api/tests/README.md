# API test harness

Tests for the platform API, run against a real server on a temporary data
directory. Nothing is mocked. The method is described in
`docs/plans/platform-api-implementation.md`.

```sh
cargo build --locked -p supabricks-local
python3 -m venv api/tests/.venv
api/tests/.venv/bin/pip install -r api/tests/requirements.txt
api/tests/.venv/bin/python -m pytest api/tests -q
```

Or `just api-test`. Select one layer with `-m l3`, or one domain by file.

## Layers

| Layer | File | Written per domain |
|---|---|---|
| L0 specification | `test_l0_specification.py` | No |
| L1 conformance | every call through `Client.call` is checked against the specification | No |
| L2 scenarios | `test_<domain>.py` | Yes |
| L3 invariants | `test_l3_invariants.py`, generated from the specification | No |
| L4 authorization | not built yet; it needs the governed profile | No |
| L5 durability | `test_<domain>.py`, with the `private_server` fixture | Yes |
| L6 parity | `test_<domain>.py`, comparing with `server.rpc` or the command line | Yes |
| L7 fuzz | `test_fuzz.py` | No |

Generated layers cover an operation as soon as `api/status.yaml` marks it
`implemented`. A domain's own file must name each of its served operations, or
the coverage test fails.

## Writing a domain's tests

```python
def test_rename(api):
    project = api.call('createProject', json={'name': 'a'}, expect=201).json()
    api.call('updateProject', path={'project': project['id']}, json={'name': 'b'}, expect=200)
```

`api.call` takes an `operationId`, builds the URL from the specification and
fails if the response is not one the specification declares. `api.raw` sends
any request and still requires every error to be a problem document.

## Fixtures

| Fixture | Scope | Use |
|---|---|---|
| `server` | session | The shared server. `server.rpc(...)` speaks to the daemon as the command line does |
| `api` | session | A signed-in client |
| `anonymous` | session | A client with no session |
| `private_server` | test | A server of its own, for tests that restart or kill it |

| `engine_api` | session | A signed-in client on a server with database engines, for tests marked `engine` |

The default server is the local profile without engines. It starts in about a
second and covers everything that does not need a running PostgreSQL.

Tests marked `engine` need work to finish, so they run against a server started
with a verified engine bundle (`--bundle`, `--helpers`, or
`SUPABRICKS_ENGINE_BUNDLE` and `SUPABRICKS_ENGINE_HELPERS`; an installed release
is found automatically). They are skipped where there is no bundle, which today
includes the `api` workflow in CI. Wiring the bundle into CI is part of AP02.
