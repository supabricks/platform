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
| L4 authorization | `test_l4_authorization.py`, generated from `x-required-role` and `x-profiles` | No |
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
| `governed` | session | A server that also serves signed-in callers. `governed.callers` has one client for each position: `nobody`, `outsider`, `viewer`, `editor`, `administrator` (of `governed.project`) and `elsewhere` (administrator of `governed.other_project`) |

| `engine_server`, `engine_api` | session | A server with database engines and a signed-in client on it, for tests marked `engine` |

The default server is the local profile without engines. It starts in about a
second and covers everything that does not need a running PostgreSQL.

Tests marked `engine` need work to finish, so they run against a server started
with a verified engine bundle (`--bundle`, `--helpers`, or
`SUPABRICKS_ENGINE_BUNDLE` and `SUPABRICKS_ENGINE_HELPERS`; an installed release
is found automatically). They are skipped where there is no bundle. The
`engines` job of the `api` workflow runs them on Linux and macOS with
`--require-engines`, which fails instead of skipping.

Tests marked `worker` upload or inspect a file, which the import worker reads
(`python/ingest/worker.py`). It needs a Python with the packages in
`requirements-worker.txt`: the one given as `--worker-python` or
`SUPABRICKS_WORKER_PYTHON`, else an installed release's, else the Python
running the tests. They are skipped where none has the packages; CI installs
them and passes `--require-worker`. A test that loads a file is marked `engine`
as well.

An installation runs at most 32 branches, and without engines a deleted branch
keeps its place. A domain that creates databases or branches therefore uses a
server of its own (see `test_databases.py`, `test_branches.py`, `test_sql.py`, `test_tables.py`, `test_imports.py`, `test_roles.py`, `test_extensions.py`, `test_metrics.py` and `test_activity.py`) and shares one resource between
tests that do not change it.
