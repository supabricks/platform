"""Disposable TLS OIDC fixture; keys here are public test material, never deploy.

Uses actual RS256 signatures and PKCE validation. No external Python packages.
"""
import base64
import hashlib
import http.server
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import urllib.parse

ROOT = Path(__file__).parent
codes = {}
tokens = {}
active = True
outage = False
key_id = 'fixture'


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def signed(claims, alg='RS256'):
    body = '.'.join(b64(json.dumps(v).encode()) for v in (
        dict(alg=alg, kid=key_id), claims))
    signature = subprocess.check_output(
        ['openssl', 'dgst', '-sha256', '-sign', str(ROOT / 'key.pem')], input=body.encode())
    return body + '.' + b64(signature)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global active, outage, key_id
        url = urllib.parse.urlsplit(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if url.path == '/control':
            active = query.get('active', 'true') == 'true'
            outage = query.get('outage', 'false') == 'true'
            key_id = query.get('kid', key_id)
            return self.reply(200, {})
        if outage:
            return self.reply(503, {})
        if url.path == '/.well-known/openid-configuration':
            return self.reply(200, dict(
                issuer=issuer, authorization_endpoint=issuer+'/authorize',
                token_endpoint=issuer+'/token', userinfo_endpoint=issuer+'/userinfo',
                jwks_uri=issuer+'/jwks', response_types_supported=['code'],
                subject_types_supported=['public'], id_token_signing_alg_values_supported=['RS256']))
        if url.path == '/jwks':
            keys = json.loads((ROOT/'jwks.json').read_text())
            keys['keys'][0]['kid'] = key_id
            return self.reply(200, keys)
        if url.path == '/authorize':
            if query.get('code_challenge_method') != 'S256' or not query.get('nonce'):
                return self.reply(400, {})
            code = secrets.token_hex(20)
            codes[code] = query
            location = query['redirect_uri']+'?'+urllib.parse.urlencode(dict(code=code, state=query['state']))
            return self.reply(302, {}, {'Location': location})
        if url.path == '/userinfo':
            claims = tokens.get(self.headers.get('Authorization', '').removeprefix('Bearer '))
            if not claims:
                return self.reply(401, {})
            return self.reply(200, dict(sub=claims['sub'], email=claims['email']))
        return self.reply(404, {})

    def do_POST(self):
        if outage:
            return self.reply(503, {})
        query = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers['Content-Length'])).decode()))
        if self.path == '/introspect':
            claims = tokens.get(query.get('token'))
            return self.reply(200, dict(claims, active=active) if claims else dict(active=False))
        if self.path != '/token':
            return self.reply(404, {})
        auth = codes.pop(query.get('code'), None)
        if (not auth or b64(hashlib.sha256(query.get('code_verifier', '').encode()).digest()) != auth['code_challenge']
                or query.get('redirect_uri') != auth['redirect_uri']):
            return self.reply(400, dict(error='invalid_grant'))
        now = int(time.time())
        claims = dict(iss=issuer, aud='platform', sub=auth.get('fixture_subject', 'alice'),
                      email=auth.get('fixture_email', 'alice@example.test'), iat=now, exp=now+300,
                      nonce=auth['nonce'])
        case = auth.get('fixture_case', '')
        if case == 'issuer':
            claims['iss'] = 'https://wrong.example'
        if case == 'audience':
            claims['aud'] = 'other'
        if case == 'nonce':
            claims['nonce'] = 'wrong'
        if case == 'expired':
            claims['exp'] = now-60
        token = secrets.token_hex(32)
        tokens[token] = claims
        jwt = signed(claims, 'HS256' if case == 'algorithm' else 'RS256')
        if case == 'signature':
            jwt = jwt[:-32] + 'A'*32
        return self.reply(200, dict(access_token=token, token_type='Bearer', expires_in=300, id_token=jwt))


server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
issuer = 'https://127.0.0.1:' + str(server.server_port)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(str(ROOT/'cert.pem'), str(ROOT/'key.pem'))
server.socket = context.wrap_socket(server.socket, server_side=True)
print(issuer, flush=True)
server.serve_forever()
