"""Fixtures for the API test harness. See README.md in this directory."""
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from harness.server import EngineServer, GovernedServer, Server  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption('--binary', default=str(REPO / 'target/debug/supabricks'),
                     help='the supabricks executable to test')
    installed = pathlib.Path.home() / '.local/share/supabricks/current'
    parser.addoption('--bundle', default=os.environ.get('SUPABRICKS_ENGINE_BUNDLE', str(installed / 'engine')),
                     help='a verified engine bundle, for tests marked engine')
    parser.addoption('--helpers', default=os.environ.get('SUPABRICKS_ENGINE_HELPERS', str(installed / 'helpers')),
                     help='prepared engine helpers, for tests marked engine')
    parser.addoption('--require-engines', action='store_true',
                     help='fail, instead of skipping, when the engine bundle is missing')


def pytest_configure(config):
    for layer, meaning in [('l0', 'specification'), ('l1', 'conformance and coverage'), ('l2', 'scenarios'),
                           ('l3', 'generated invariants'), ('l4', 'authorization by role'), ('l5', 'durability'), ('l6', 'parity with the command line'), ('l7', 'fuzz'),
                           ('engine', 'needs database engines; skipped without a bundle')]:
        config.addinivalue_line('markers', f'{layer}: {meaning}')


@pytest.fixture(scope='session')
def server(request):
    binary = pathlib.Path(request.config.getoption('--binary')).resolve()
    if not binary.exists():
        pytest.exit(f'{binary} does not exist; run cargo build -p supabricks-local', returncode=2)
    server = Server(binary).start()
    yield server
    server.stop()
    if request.session.testsfailed:
        sys.stderr.write(f'\n--- daemon log ({server.root}) ---\n{server.log()[-4000:]}\n')
    server.cleanup()


@pytest.fixture(scope='session')
def engine_server(request):
    """A server with database engines. Tests that use it are skipped where no
    engine bundle is available."""
    bundle = pathlib.Path(request.config.getoption('--bundle')).resolve()
    helpers = pathlib.Path(request.config.getoption('--helpers')).resolve()
    if not (bundle / 'manifest.json').exists() or not helpers.is_dir():
        if request.config.getoption('--require-engines'):
            pytest.fail(f'no engine bundle at {bundle} with helpers at {helpers}')
        pytest.skip('no engine bundle; pass --bundle and --helpers')
    server = EngineServer(pathlib.Path(request.config.getoption('--binary')).resolve(), bundle, helpers).start()
    yield server
    server.stop()
    server.cleanup()


@pytest.fixture(scope='session')
def engine_api(engine_server):
    """A signed-in client on the server with database engines."""
    return engine_server.session()


@pytest.fixture(scope='session')
def api(server):
    """A signed-in client."""
    return server.session()


@pytest.fixture(scope='session')
def anonymous(server):
    """A client with no session."""
    return server.anonymous()


@pytest.fixture
def private_server(request):
    """A server of its own, for tests that restart or kill it."""
    server = Server(pathlib.Path(request.config.getoption('--binary')).resolve()).start()
    yield server
    server.stop()
    server.cleanup()


class Governed:
    """A governed server with two projects and one caller for each position a
    person can hold: no session, no role anywhere, each role in the first
    project, and administrator of the second project only."""

    def __init__(self, server):
        self.server = server
        owner = server.session()

        def create(name):
            import uuid
            body = owner.call('createProject', json=dict(name=name), expect=202,
                              headers={'Idempotency-Key': str(uuid.uuid4())}).json()
            return body['project_id'], body['id']
        self.project, self.operation = create('roles-one')
        self.other_project, _ = create('roles-two')
        import uuid
        self.database = owner.call('createDatabase', path=dict(project=self.project), json=dict(name='roles-db'),
                                   headers={'Idempotency-Key': str(uuid.uuid4())}, expect=202).json()['target']['id']
        self.ids, self.callers = {}, {'nobody': server.bearer(None)}
        for name, project, role in [('outsider', None, None), ('viewer', self.project, 'viewer'),
                                    ('editor', self.project, 'editor'),
                                    ('administrator', self.project, 'administrator'),
                                    ('elsewhere', self.other_project, 'administrator')]:
            self.ids[name], self.callers[name] = server.principal(name)
            if project:
                server.grant(project, self.ids[name], role)


@pytest.fixture(scope='session')
def governed(request):
    server = GovernedServer(pathlib.Path(request.config.getoption('--binary')).resolve()).start()
    yield Governed(server)
    server.stop()
    if request.session.testsfailed:
        sys.stderr.write(f'\n--- governed daemon log ({server.root}) ---\n{server.log()[-4000:]}\n')
    server.cleanup()
