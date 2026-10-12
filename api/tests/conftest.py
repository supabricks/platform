"""Fixtures for the API test harness. See README.md in this directory."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from harness.server import Server  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]


def pytest_addoption(parser):
    parser.addoption('--binary', default=str(REPO / 'target/debug/supabricks'),
                     help='the supabricks executable to test')


def pytest_configure(config):
    for layer, meaning in [('l0', 'specification'), ('l1', 'conformance and coverage'), ('l2', 'scenarios'),
                           ('l3', 'generated invariants'), ('l5', 'durability'), ('l7', 'fuzz')]:
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
