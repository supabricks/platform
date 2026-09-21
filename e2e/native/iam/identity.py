"""Actual Keycloak code/PKCE, UC token exchange, and a private broker prototype."""
import base64
import hashlib
import html
import http.cookiejar
import json
import secrets
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from common import HERE, PINS, command, request, wait


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Keycloak:
    def __init__(self, root, name):
        self.root, self.name = root, name
        self.password = secrets.token_urlsafe(32)
        self.secret = secrets.token_urlsafe(32)
        self.admin_password = secrets.token_urlsafe(32)
        self.started = False

    def start(self):
        audience = dict(name='probe-audience', protocol='openid-connect',
            protocolMapper='oidc-audience-mapper', config={
                'included.client.audience': 'supabricks', 'access.token.claim': 'true'})
        introspection_audience = dict(name='introspection-audience', protocol='openid-connect',
            protocolMapper='oidc-audience-mapper', config={
                'included.client.audience': 'service', 'access.token.claim': 'true'})
        clients = [dict(clientId=name, enabled=True, publicClient=True,
            standardFlowEnabled=True, directAccessGrantsEnabled=False,
            redirectUris=['http://127.0.0.1:1/callback'],
            protocolMappers=[] if name == 'wrong-audience' else [audience, introspection_audience])
            for name in ('supabricks', 'wrong-audience')]
        clients.append(dict(clientId='service', enabled=True, secret=self.secret,
            serviceAccountsEnabled=True, standardFlowEnabled=False,
            directAccessGrantsEnabled=False, protocolMappers=[audience]))
        # A signed external email label equal to UC's reserved admin string.
        clients.append(dict(clientId='admin-label', enabled=True, publicClient=True,
            standardFlowEnabled=True, directAccessGrantsEnabled=False,
            redirectUris=['http://127.0.0.1:1/callback'], defaultClientScopes=["basic"], optionalClientScopes=[], protocolMappers=[audience, dict(
                name='external-admin-label', protocol='openid-connect',
                protocolMapper='oidc-hardcoded-claim-mapper', config={
                    'claim.name': 'email', 'claim.value': 'admin',
                    'jsonType.label': 'String', 'access.token.claim': 'true'})]))
        realm = dict(realm='iam00', enabled=True, sslRequired='none',
            duplicateEmailsAllowed=True, loginWithEmailAllowed=False,
            accessTokenLifespan=600, clients=clients,
            users=[dict(username=name, enabled=True, email=email, emailVerified=True,
                firstName=name, lastName='Probe', credentials=[dict(type='password',
                    value=self.password, temporary=False)]) for name, email in (
                        ('alice', 'alice@example.test'), ('bob', 'bob@example.test'),
                        ('same-email', 'alice@example.test'))])
        path = self.root / 'realm.json'
        path.write_text(json.dumps(realm)); path.chmod(0o644)
        command('docker', 'run', '-d', '--name', self.name, '--label', 'supabricks.iam00=true',
            '--memory=1g', '--cpus=2', '--pids-limit=256', '-p', '127.0.0.1::8080',
            '-v', f'{path}:/opt/keycloak/data/import/realm.json:ro',
            '-e', 'KC_BOOTSTRAP_ADMIN_USERNAME=probe-admin',
            '-e', 'KC_BOOTSTRAP_ADMIN_PASSWORD=' + self.admin_password,
            PINS['keycloak']['image'], 'start-dev', '--import-realm')
        self.started = True
        port = json.loads(command('docker', 'inspect', self.name))[0]['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
        self.base = 'http://127.0.0.1:' + port
        self.issuer = self.base + '/realms/iam00'
        wait(lambda: request(self.issuer + '/.well-known/openid-configuration')[0] == 200, 90)
        self.jwks = request(self.issuer + '/protocol/openid-connect/certs')[1]

    def login(self, username, client='supabricks'):
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(24)
        query = urllib.parse.urlencode(dict(client_id=client, redirect_uri='http://127.0.0.1:1/callback',
            response_type='code', scope='openid' if client == 'admin-label' else 'openid email', state=state,
            code_challenge=b64(hashlib.sha256(verifier.encode()).digest()), code_challenge_method='S256'))
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(jar), NoRedirect())
        status, page, _ = request(self.issuer + '/protocol/openid-connect/auth?' + query, opener=opener)
        assert status == 200, 'code/PKCE login page unavailable'
        from html.parser import HTMLParser
        class Form(HTMLParser):
            action = None
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form' and attrs.get('id') == 'kc-form-login':
                    self.action = attrs['action']
        form = Form(); form.feed(page)
        # Browser loopback is a secure context; urllib does not implement that exception.
        # This fixture has an exact loopback URL and is never an application client.
        for cookie in jar:
            cookie.secure = False
        assert form.action, 'missing Keycloak login form'
        status, result, headers = request(html.unescape(form.action), 'POST',
            dict(username=username, password=self.password, credentialId=''), form=True, opener=opener)
        if status != 302:
            (self.root / 'login-failure.html').write_text(str(result))
        assert status == 302, 'code/PKCE login failed'
        redirect = urllib.parse.urlparse(headers['Location'])
        assert redirect.scheme == 'http' and redirect.netloc == '127.0.0.1:1' and redirect.path == '/callback'
        values = urllib.parse.parse_qs(redirect.query)
        assert values['state'] == [state]
        status, body, _ = request(self.issuer + '/protocol/openid-connect/token', 'POST',
            dict(grant_type='authorization_code', code=values['code'][0], client_id=client,
                 redirect_uri='http://127.0.0.1:1/callback', code_verifier=verifier), form=True)
        assert status == 200, 'code/PKCE token exchange failed'
        return body['access_token']

    def service(self):
        status, body, _ = request(self.issuer + '/protocol/openid-connect/token', 'POST',
            dict(grant_type='client_credentials', client_id='service', client_secret=self.secret), form=True)
        assert status == 200
        return body['access_token']

    def admin(self, method, path, body=None):
        # Only the disposable fixture bootstrap uses its admin CLI grant. User flows use PKCE.
        status, token, _ = request(self.base + '/realms/master/protocol/openid-connect/token', 'POST',
            dict(grant_type='password', client_id='admin-cli', username='probe-admin',
                 password=self.admin_password), form=True)
        assert status == 200
        result = request(self.base + '/admin/realms/iam00/' + path, method, body, token['access_token'])
        assert 200 <= result[0] < 300, 'fixture administration failed'
        return result[1]

    def introspect(self, token):
        status, body, _ = request(self.issuer + '/protocol/openid-connect/token/introspect', 'POST',
            dict(client_id='service', client_secret=self.secret, token=token), form=True)
        assert status == 200
        return body['active']

    def stop(self):
        if self.started:
            command('docker', 'rm', '-f', self.name)
            self.started = False


class Broker:
    """Proof only: persisted opaque IDs; externally signed claims never select UC email."""
    def __init__(self, path, idp):
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS subjects(issuer TEXT, subject TEXT, id TEXT UNIQUE, PRIMARY KEY(issuer,subject))')
        self.idp = idp

    def claims(self, token):
        head, body, signature = token.split('.')
        header, claims = json.loads(decode(head)), json.loads(decode(body))
        if header.get('alg') != 'RS256' or claims.get('iss') != self.idp.issuer:
            raise ValueError('issuer/algorithm rejected')
        audiences = claims.get('aud', [])
        if isinstance(audiences, str): audiences = [audiences]
        if 'supabricks' not in audiences or not claims.get('sub') or claims['exp'] <= time.time():
            raise ValueError('audience/subject/expiry rejected')
        key = next(k for k in self.idp.jwks['keys'] if k['kid'] == header['kid'] and k['alg'] == 'RS256')
        public = rsa.RSAPublicNumbers(int.from_bytes(decode(key['e']), 'big'),
                                     int.from_bytes(decode(key['n']), 'big')).public_key()
        try:
            public.verify(decode(signature), (head + '.' + body).encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as error:
            raise ValueError('signature rejected') from error
        return claims

    def admit(self, token):
        # No positive introspection cache in the probe: an unavailable provider fences admission.
        principal = self.principal(token)
        try:
            if self.idp.introspect(token): return principal
        except (OSError, ValueError, AssertionError):
            pass
        raise ValueError('inactive identity or unavailable provider')

    def principal(self, token):
        claims = self.claims(token)
        key = (claims['iss'], claims['sub'])
        self.db.execute('INSERT OR IGNORE INTO subjects VALUES(?,?,?)', (*key, str(uuid.uuid4())))
        self.db.commit()
        return self.db.execute('SELECT id FROM subjects WHERE issuer=? AND subject=?', key).fetchone()[0]


def exchange(server, token):
    return request(server.base + '/api/1.0/unity-control/auth/tokens', 'POST', dict(
        grant_type='urn:ietf:params:oauth:grant-type:token-exchange',
        requested_token_type='urn:ietf:params:oauth:token-type:access_token',
        subject_token_type='urn:ietf:params:oauth:token-type:access_token', subject_token=token), form=True)


def probe(idp, server, root, evidence, release=None):
    broker = Broker(root / 'identities.sqlite3', idp)
    try:
        tokens = {name: idp.login(name) for name in ('alice', 'bob', 'same-email')}
        tokens['service'] = idp.service()
        ids = {name: broker.principal(token) for name, token in tokens.items()}
        evidence.check('signed_oidc_subjects_and_equal_emails_are_distinct', len(set(ids.values())) == 4)
        broker.db.close()
        broker = Broker(root / 'identities.sqlite3', idp)
        evidence.check('stable_mapping_survives_reopen', all(broker.principal(t) == ids[n] for n, t in tokens.items()))
        for name, token in tokens.items():
            server.user('p-' + ids[name] + '@iam00.invalid')
        alice = 'p-' + ids['alice'] + '@iam00.invalid'
        if release:
            command(release/'python/analytics/python','-c',
                'import sys,pyarrow as pa; from deltalake import write_deltalake; write_deltalake(sys.argv[1],pa.table({"id":[42]}))',root/'alice-delta')
        server.ok('POST', 'catalogs', dict(name='alice'))
        server.ok('POST', 'schemas', dict(name='data', catalog_name='alice'))
        server.ok('POST', 'tables', dict(name='private', catalog_name='alice', schema_name='data',
            table_type='EXTERNAL', data_source_format='DELTA', storage_location=(root/'alice-delta').as_uri(),
            columns=[dict(name='id', type_name='LONG', type_text='bigint', position=0,
                type_json=json.dumps(dict(name='id',type='long',nullable=True,metadata={})),nullable=True)]))
        server.grant('catalog', 'alice', alice, ['USE CATALOG'])
        server.grant('schema', 'alice.data', alice, ['USE SCHEMA'])
        server.grant('table', 'alice.data.private', alice, ['SELECT'])
        for name in ids:
            token = server.token('p-' + ids[name] + '@iam00.invalid')
            status, _ = server.request('GET', 'tables/alice.data.private', token=token)
            evidence.check('uc_table_access_' + name, status == 200 if name == 'alice' else status in (403,404), http=status)
            if name != 'alice':
                catalogs = server.ok('GET', 'catalogs', token=token)
                evidence.check('uc_metadata_filter_' + name, 'alice' not in [c['name'] for c in catalogs.get('catalogs',[])])
        if release:
            for name in ('alice','bob','service'):
                result=subprocess.run([str(release/'python/analytics/python'),str(HERE/'catalog_reader.py')],
                    input=json.dumps(dict(uri=server.base+'/api/2.1/unity-catalog',
                        token=server.token('p-'+ids[name]+'@iam00.invalid')))+'\n',
                    capture_output=True,text=True,timeout=60)
                assert result.returncode == 0, 'private UC reader exited'
                value=json.loads(next(line[13:] for line in result.stdout.splitlines() if line.startswith('IAM00_RESULT ')))
                evidence.check('sail_uc_table_read_'+name,value==dict(ok=True,rows=[dict(id=42)]) if name=='alice' else not value['ok'])
        admin_token = idp.login('bob', 'admin-label')
        evidence.check('signed_external_admin_label_positive_control', broker.claims(admin_token).get('email') == 'admin')
        evidence.check('external_admin_label_has_no_broker_elevation', broker.principal(admin_token) == ids['bob'])
        status, _ = server.request('GET', 'catalogs', token=server.token('p-' + broker.principal(admin_token) + '@iam00.invalid'))
        evidence.check('broker_admin_label_is_ordinary_principal', status == 200)
        status, _ = server.request('POST','catalogs',dict(name='bob_cannot_create'),
            token=server.token('p-'+broker.principal(admin_token)+'@iam00.invalid'))
        evidence.check('broker_admin_label_cannot_create_catalog',status==403,http=status)
        for name, token in [('wrong_audience', idp.login('alice', 'wrong-audience')),
                            ('forged_issuer', tokens['alice'].split('.')[0] + '.' + b64(json.dumps(
                                dict(broker.claims(tokens['alice']), iss='https://untrusted.invalid')).encode()) + '.' + tokens['alice'].split('.')[2])]:
            try: broker.principal(token)
            except ValueError: rejected = True
            else: rejected = False
            evidence.check('broker_rejects_' + name, rejected)
            status, _, _ = exchange(server, token)
            evidence.check('uc_exchange_rejects_' + name, status in (400,401,403), http=status)
        # Characterize the native path rather than claiming the adapter fixes upstream.
        server.user('alice@example.test')
        native = {}
        for name in ('alice','same-email'):
            status, body, _ = exchange(server, tokens[name])
            evidence.check('native_exchange_' + name, status == 200, http=status)
            native[name] = json.loads(decode(body['access_token'].split('.')[1]))['sub']
        evidence.limit('native_uc_email_identity_collision', same_uc_subject=native['alice'] == native['same-email'])
        assert native['alice'] == native['same-email']
        status, body, _ = exchange(server, admin_token)
        evidence.check('native_external_admin_label_exchange_observed', status == 200, http=status)
        status, _ = server.request('POST','catalogs', dict(name='external_admin_created'), token=body['access_token'])
        assert status == 200, 'native admin-label escalation changed; reassess no-go'
        evidence.limit('native_external_admin_label_not_safe_for_direct_federation', create_catalog_http=status)
        group_status, group_body = server.request('POST', 'scim2/Groups', dict(displayName='analysts'), control=True)
        (root / 'group-response.json').write_text(json.dumps(dict(status=group_status, body=group_body)))
        evidence.check('native_group_endpoint_characterized', group_status == 500 and group_body.get('message') == "Couldn't unwrap service.", http=group_status)
        evidence.limit('native_groups_unavailable_use_materialized_direct_grants', http=group_status)
        alice_subject = broker.claims(tokens['alice'])['sub']
        idp.admin('PUT', 'users/' + alice_subject, dict(email='changed@example.test'))
        evidence.check('email_change_preserves_principal', broker.principal(idp.login('alice')) == ids['alice'])
        idp.admin('DELETE', 'users/' + alice_subject)
        idp.admin('POST', 'users', dict(username='alice', enabled=True, email='alice@example.test',
            emailVerified=True, firstName='Alice', lastName='Recreated', credentials=[dict(
                type='password', value=idp.password, temporary=False)]))
        new_id = broker.principal(idp.login('alice'))
        evidence.check('recreated_subject_does_not_inherit_identity', new_id != ids['alice'])
        server.user('p-' + new_id + '@iam00.invalid')
        status, _ = server.request('GET','tables/alice.data.private',token=server.token('p-' + new_id + '@iam00.invalid'))
        evidence.check('recreated_subject_does_not_inherit_grants', status in (403,404), http=status)
        token = tokens['bob']
        evidence.check('introspection_active_positive_control', idp.introspect(token) and broker.admit(token)==ids['bob'])
        began = time.monotonic()
        idp.admin('PUT', 'users/' + broker.claims(token)['sub'], dict(enabled=False))
        wait(lambda: not idp.introspect(token), 20)
        elapsed = time.monotonic() - began
        evidence.check('idp_disable_detected_before_jwt_expiry', elapsed < 300 and broker.claims(token)['exp'] > time.time(), seconds=round(elapsed,3))
        evidence.metrics['idp_disable_seconds'] = round(elapsed,3)
        try: broker.admit(token)
        except ValueError: denied=True
        else: denied=False
        evidence.check('disabled_identity_admission_denied',denied)
        live=idp.login('alice')
        evidence.check('recreated_identity_admission_positive_control',broker.admit(live)==new_id)
        command('docker','pause',idp.name)
        try:
            try: broker.admit(live)
            except ValueError: denied=True
            else: denied=False
            evidence.check('idp_outage_fences_admission',denied)
        finally: command('docker','unpause',idp.name)
        evidence.check('idp_recovery_requires_active_identity',broker.admit(live)==new_id)
        head,body,signature=live.split('.')
        forged=head+'.'+body+'.'+('A' if signature[0]!='A' else 'B')+signature[1:]
        try: broker.principal(forged)
        except ValueError: denied=True
        else: denied=False
        evidence.check('forged_signature_rejected',denied)
    finally:
        broker.db.close()
