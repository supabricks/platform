#!/usr/bin/env python3
"""Qualify the actual UC09.1 CLI against IAM00's pinned Keycloak over TLS.

Only disposable resources are created. The fixture uses development Keycloak;
this is login qualification, not shared-server or installed-release acceptance.
"""
import argparse
import contextlib
import hashlib
import http.cookiejar
from html.parser import HTMLParser
import json
from pathlib import Path
import secrets
import select
import socket
import ssl
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

REPO = Path(__file__).resolve().parents[3]
PINS = json.loads((REPO/'e2e/native/iam/pins.lock.json').read_text())
FIXTURE = REPO/'crates/local/tests/fixtures/identity'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class Form(HTMLParser):
    action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form' and attrs.get('id') == 'kc-form-login':
            self.action = attrs['action']


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run(args, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, timeout=120, **kwargs)
    if result.returncode:
        raise RuntimeError('qualification command failed: ' + str(args[0]))
    return result.stdout.decode()


def qualify(binary, uc_runtime=None, execution_config=None):
    checks = []
    measurements = {}
    name = 'sb-uc091-' + uuid.uuid4().hex[:12]
    started = False
    daemon = None
    logins = []
    with tempfile.TemporaryDirectory(prefix='sb-uc091-') as directory:
        root = Path(directory)
        data = root/'data'
        callback = f'http://127.0.0.1:{port()}/auth/v1/callback'
        base = f'https://127.0.0.1:{port()}'
        issuer = base+'/realms/uc091'
        password, secret, admin_password = (secrets.token_urlsafe(32) for _ in range(3))
        audience = dict(name='platform-audience', protocol='openid-connect',
                        protocolMapper='oidc-audience-mapper',
                        config={'included.client.audience': 'platform', 'access.token.claim': 'true'})
        realm = dict(realm='uc091', enabled=True, sslRequired='all',
                     duplicateEmailsAllowed=True, loginWithEmailAllowed=False,
                     clients=[dict(clientId='platform', enabled=True, publicClient=False,
                                   secret=secret, standardFlowEnabled=True, directAccessGrantsEnabled=False,
                                   redirectUris=[callback], protocolMappers=[audience])],
                     users=[dict(username=user, enabled=True, email=email, emailVerified=True,
                                 firstName=user, lastName='Fixture',
                                 credentials=[dict(type='password', value=password, temporary=False)])
                            for user, email in [('alice', 'shared@example.test'), ('bob', 'shared@example.test')]])
        realm_file = root/'realm.json'
        realm_file.write_text(json.dumps(realm)); realm_file.chmod(0o644)
        env = root/'keycloak.env'
        env.write_text('KC_BOOTSTRAP_ADMIN_USERNAME=fixture-admin\nKC_BOOTSTRAP_ADMIN_PASSWORD='+admin_password+'\n')
        env.chmod(0o600)
        # Public test material only; readable in the disposable container.
        for filename in ('cert.pem', 'key.pem'):
            path = root/filename
            path.write_bytes((FIXTURE/filename).read_bytes()); path.chmod(0o644)
        context = ssl.create_default_context(cafile=str(root/'cert.pem'))
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=context),
                    urllib.request.HTTPCookieProcessor(jar), NoRedirect())

        def request(url, method='GET', body=None, token=None, form=False):
            headers = {}
            if body is not None:
                body = urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode()
                headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            if token:
                headers['Authorization'] = 'Bearer '+token
            try:
                response = opener.open(urllib.request.Request(url, body, headers, method=method), timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                text = response.read().decode()
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    value = text
                return response.status, value, response.headers

        def cli(*args, ok=True):
            result = subprocess.run([str(binary), *map(str, args), '--data-dir', str(data)],
                                    capture_output=True, timeout=40)
            assert (result.returncode == 0) == ok, 'unexpected CLI result for '+str(args[0])
            return json.loads(result.stdout) if ok else None

        def admin(command, output=None):
            path = root/'request.json'; path.write_text(json.dumps(command)); path.chmod(0o600)
            return cli('identity', 'admin', '--request-file', path,
                       *(['--output', output] if output else []))

        def login(username, suffix):
            output = root/(suffix+'.json')
            process = subprocess.Popen([str(binary), 'identity', 'login', '--provider', 'keycloak',
                '--redirect', callback, '--output', str(output), '--data-dir', str(data)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            logins.append(process)
            assert select.select([process.stderr], [], [], 20)[0], 'login startup timed out'
            line = process.stderr.readline().strip()
            assert line.startswith('Open this URL to sign in: '), 'missing authorization URL'
            status, page, _ = request(line.split(': ', 1)[1])
            assert status == 200, 'Keycloak login page unavailable'
            form = Form(); form.feed(page)
            assert form.action, 'missing Keycloak login form'
            status, _, headers = request(form.action, 'POST',
                dict(username=username, password=password, credentialId=''), form=True)
            assert status == 302, 'Keycloak login refused'
            assert headers['Location'].startswith(callback+'?'), 'unexpected redirect'
            status, _, _ = request(headers['Location'])
            assert status == 200, 'platform callback refused'
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, 'platform code exchange failed'
            session = json.loads(output.read_text())
            assert session['token'] not in stdout+stderr and 'access_token' not in session
            assert output.stat().st_mode & 0o777 == 0o600
            return output, json.loads(stdout)['principal_id']

        def keycloak_admin(method, path, body=None):
            status, token, _ = request(base+'/realms/master/protocol/openid-connect/token', 'POST',
                dict(grant_type='password', client_id='admin-cli', username='fixture-admin',
                     password=admin_password), form=True)
            assert status == 200
            status, body, _ = request(base+'/admin/realms/uc091/'+path, method, body, token['access_token'])
            assert 200 <= status < 300
            return body

        try:
            run(['docker', 'run', '-d', '--name', name, '--memory=1g', '--cpus=2', '--pids-limit=256',
                 '-p', '127.0.0.1:'+base.rsplit(':', 1)[1]+':8443', '--env-file', env,
                 '-v', str(realm_file)+':/opt/keycloak/data/import/realm.json:ro',
                 '-v', str(root/'cert.pem')+':/tls/cert.pem:ro', '-v', str(root/'key.pem')+':/tls/key.pem:ro',
                 PINS['keycloak']['image'], 'start-dev', '--import-realm', '--http-enabled=false',
                 '--https-certificate-file=/tls/cert.pem', '--https-certificate-key-file=/tls/key.pem',
                 '--hostname='+base])
            started = True
            deadline = time.monotonic()+100
            while True:
                try:
                    if request(issuer+'/.well-known/openid-configuration')[0] == 200:
                        break
                except (OSError, urllib.error.URLError):
                    pass
                assert time.monotonic()<deadline, 'Keycloak startup timed out'
                time.sleep(1)
            daemon = subprocess.Popen([str(binary), 'daemon', '--data-dir', str(data)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic()+10
            while not (data/'control.sock').exists():
                assert time.monotonic()<deadline
                time.sleep(.05)
            admin(dict(action='configure', provider='keycloak', config=dict(issuer=issuer,
                client_id='platform', client_secret=secret, introspection_url=issuer+'/protocol/openid-connect/token/introspect',
                redirects=[callback], ca_pem=(root/'cert.pem').read_text())))
            alice, alice_id = login('alice', 'alice')
            # A fresh browser cookie jar forces a second human login.
            jar.clear()
            bob, bob_id = login('bob', 'bob')
            assert alice_id != bob_id
            assert cli('identity', 'whoami', '--session-file', alice)['actor_id'] == alice_id
            assert cli('identity', 'whoami', '--session-file', bob)['actor_id'] == bob_id
            checks.append('real TLS Keycloak PKCE logins and equal-email principal separation')
            # Exercise the same authenticated authorization path used by the
            # browser adapter and MCP, against the two real Keycloak sessions.
            runtime_project = str(uuid.uuid4())
            with socket.socket(socket.AF_UNIX) as control_socket:
                control_socket.settimeout(10)
                control_socket.connect(str(data/'control.sock'))
                control_socket.sendall((json.dumps(dict(version=1, request=dict(
                    method='register_project', config=dict(format_version=1,
                    id=runtime_project, name='governed-fixture'))))+'\n').encode())
                with control_socket.makefile('rb') as reader:
                    assert 'result' in json.loads(reader.readline())
            with sqlite3.connect('file:'+str(data/'state.sqlite3')+'?mode=ro', uri=True) as db:
                deployment = db.execute('SELECT id FROM deployments WHERE runtime_project_id=?',
                                        (runtime_project,)).fetchone()[0]

            def policy(command):
                path = root/'policy.json'; path.write_text(json.dumps(command)); path.chmod(0o600)
                return cli('identity', 'policy-admin', '--request-file', path)

            def update(action, **fields):
                revision = policy(dict(action='policy', deployment=deployment))['policy_revision']
                return policy(dict(action=action, deployment=deployment, expected_policy=revision,
                                   key=uuid.uuid4().hex, **fields))

            def control(session, command, ok=True):
                path = root/'control.json'; path.write_text(json.dumps(command)); path.chmod(0o600)
                return cli('identity', 'control', '--request-file', path, '--session-file', session, ok=ok)

            subject = lambda principal: dict(kind='principal', id=principal)
            update('set_role', subject=subject(alice_id), role='editor')
            update('set_role', subject=subject(bob_id), role='viewer')
            revision = policy(dict(action='policy', deployment=deployment))['policy_revision']
            source_command = dict(action='save_source', deployment=deployment, asset='query',
                                  kind='sql', contents='select 1', expected_head=None,
                                  expected_policy=revision, key='first-source')
            control(bob, source_command, ok=False)
            source = control(alice, source_command)['revision']
            admission = dict(action='admit_execution', deployment=deployment, source_revision=source,
                             effective_principal=None, expected_policy=revision, key='run')
            control(alice, admission, ok=False)
            update('set_grant', subject=subject(alice_id), grant='execute',
                   effective_principal=None, source_revision=None, present=True)
            admission['expected_policy'] = policy(dict(action='policy', deployment=deployment))['policy_revision']
            admitted = control(alice, admission)
            assert admitted['actor_id'] == alice_id and admitted['effective_principal_id'] == alice_id
            assert admitted['runtime_started'] is False
            assert control(alice, admission) == admitted
            control(bob, dict(action='stop_execution', deployment=deployment, id=admitted['id'],
                              expected_policy=admission['expected_policy'], key='stop-other'), ok=False)
            for operation in ('postgres_read', 'catalog_read', 'backup', 'restore', 'web_socket', 'workload_launch'):
                control(alice, dict(action='unavailable', deployment=deployment, operation=operation), ok=False)
            checks.append('two real users enforce conflicting roles, explicit execution, idempotency and stop ownership')
            elevated = admin(dict(action='service', label='approved-worker'))['principal_id']
            update('set_role', subject=subject(elevated), role='viewer')
            update('set_grant', subject=subject(elevated), grant='execute',
                   effective_principal=None, source_revision=None, present=True)
            update('set_grant', subject=subject(alice_id), grant='act_as',
                   effective_principal=elevated, source_revision=source, present=True)
            revision = policy(dict(action='policy', deployment=deployment))['policy_revision']
            delegated = dict(admission, effective_principal=elevated, expected_policy=revision, key='approved')
            assert control(alice, delegated)['effective_principal_id'] == elevated
            edited = control(alice, dict(source_command, contents='select secret', expected_head=source,
                                         expected_policy=revision, key='edited-source'))['revision']
            control(alice, dict(delegated, source_revision=edited, key='confused-deputy'), ok=False)
            checks.append('service execution approval binds the exact immutable source and rejects edited code')

            if uc_runtime:
                def owner(request):
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.settimeout(50); sock.connect(str(data/'control.sock'))
                        sock.sendall((json.dumps(dict(version=1, request=request))+'\n').encode())
                        with sock.makefile('rb') as reader:
                            reply = json.loads(reader.readline())
                    assert 'result' in reply, 'operator catalog request failed'
                    return reply['result']

                def catalog(command):
                    path = root/'catalog.json'; path.write_text(json.dumps(command)); path.chmod(0o600)
                    return cli('identity', 'catalog-admin', '--request-file', path)

                owner(dict(method='catalog_service', command=dict(action='configure',
                    provider=dict(mode='local', runtime=str(uc_runtime)))))
                deadline = time.monotonic()+60
                while owner(dict(method='catalog_service', command=dict(action='status')))['state'] != 'ready':
                    assert time.monotonic()<deadline, 'managed UC startup timed out'
                    time.sleep(.1)
                discovery = dict(action='catalog', command=dict(action='list', search=''))
                control(alice, discovery, ok=False)
                for principal_id in (alice_id, bob_id):
                    catalog(dict(action='map_principal', principal=principal_id))
                plan = catalog(dict(action='plan', changes=[]))
                catalog(dict(action='apply', plan=plan['id'], key='initialize-catalog'))
                assert control(alice, discovery) == dict(items=[])
                assert control(bob, discovery) == dict(items=[])
                checks.append('real Keycloak users use distinct private UC mappings through the authenticated CLI and daemon; UC returns no ungranted metadata')

            if execution_config:
                assert uc_runtime, 'isolated execution requires the managed UC fixture'
                private_config = data/'execution-runtime.json'
                private_config.write_bytes(execution_config.read_bytes()); private_config.chmod(0o600)
                revision = policy(dict(action='policy', deployment=deployment))['policy_revision']
                execution = control(alice, dict(admission, expected_policy=revision, key='isolated-sql'))['id']
                launch = dict(action='runtime', deployment=deployment,
                              command=dict(action='start', id=execution, datasets=[]))
                assert control(alice, launch)['state'] == 'running'
                poll = dict(action='runtime', deployment=deployment, command=dict(action='poll', id=execution))
                control(bob, poll, ok=False)
                deadline = time.monotonic()+90
                while True:
                    result = control(alice, poll)
                    assert result['state'] != 'failed'
                    if result['state'] == 'finished':
                        assert result['result']['exit_code'] == 0
                        assert '1' in result['result']['output']
                        break
                    assert time.monotonic()<deadline
                    time.sleep(2)
                checks.append('real OIDC, CLI, daemon, project admission and UC broker launch isolated Jupyter/Sail SQL; another actor cannot read results')
                notebook = json.dumps(dict(nbformat=4, nbformat_minor=5, metadata={},
                    cells=[dict(id='qualification', cell_type='code', source=['import time; time.sleep(120)'], metadata={}, outputs=[], execution_count=None)]))
                long_source = control(alice, dict(action='save_source', deployment=deployment,
                    asset='long-notebook', kind='notebook', contents=notebook,
                    expected_head=None, expected_policy=revision, key='long-source'))['revision']
                long_execution = control(alice, dict(admission, source_revision=long_source,
                    expected_policy=revision, key='isolated-long'))['id']
                control(alice, dict(launch, command=dict(action='start', id=long_execution, datasets=[])))
                long_poll = dict(poll, command=dict(action='poll', id=long_execution))

            cli('identity', 'whoami', '--session-file', alice)
            last_success = time.monotonic()
            users = keycloak_admin('GET', 'users?username=alice')
            keycloak_admin('PUT', 'users/'+users[0]['id'], dict(enabled=False))
            disabled_at = time.monotonic()
            cli('identity', 'whoami', '--session-file', alice, ok=False)
            if uc_runtime:
                control(alice, discovery, ok=False)
            if execution_config:
                control(alice, long_poll, ok=False)
                deadline = time.monotonic()+35
                while subprocess.run(['docker','inspect','sb-exec-'+long_execution], capture_output=True).returncode == 0:
                    assert time.monotonic()<deadline, 'disabled identity execution survived renewal deadline'
                    time.sleep(.2)
                checks.append('IdP disable blocks execution renewal and the independent watchdog removes the sandbox within 35 seconds')
            measurements['idp_disable'] = dict(last_success_monotonic=last_success, acknowledged_deny_monotonic=disabled_at, observed_closed_monotonic=time.monotonic(), bound_seconds=300)
            assert measurements['idp_disable']['observed_closed_monotonic']-disabled_at < 300
            checks.append('IdP disable refuses an unexpired platform session')
            run(['docker', 'pause', name])
            try:
                cli('identity', 'whoami', '--session-file', bob, ok=False)
                cli('identity', 'logout', '--session-file', bob)
            finally:
                run(['docker', 'unpause', name])
            cli('identity', 'whoami', '--session-file', bob, ok=False)
            checks.append('provider outage fails closed while local logout remains available')
            service = admin(dict(action='service', label='automation'))['principal_id']
            credential = root/'service.json'
            admin(dict(action='issue_service', principal=service, scopes=['identity:self'], ttl_seconds=300), credential)
            assert cli('identity', 'whoami', '--session-file', credential)['actor_id'] == service
            admin(dict(action='rotate_sessions'))
            cli('identity', 'whoami', '--session-file', credential, ok=False)
            checks.append('scoped service credential and session rotation')
            events = admin(dict(action='audit', after=0))
            assert len(events['events']) >= 6
            serialized = json.dumps(events)
            assert password not in serialized and secret not in serialized
            checks.append('audited identity transitions without provider credentials')
        finally:
            for process in logins:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
            if daemon:
                daemon.terminate()
                try:
                    daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    daemon.kill(); daemon.wait(timeout=10)
            if started:
                with contextlib.suppress(Exception):
                    run(['docker', 'unpause', name])
                run(['docker', 'rm', '-f', name])
        checks.append('disposable provider, login clients and daemon cleaned up')
    return dict(status='passed', binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                keycloak=PINS['keycloak'], checks=checks, measurements=measurements,
                governed_product_ingress=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--uc-runtime', type=Path)
    parser.add_argument('--execution-config', type=Path)
    args = parser.parse_args()
    report = qualify(args.binary.resolve(), args.uc_runtime.resolve() if args.uc_runtime else None,
                     args.execution_config.resolve() if args.execution_config else None)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))
