"""N01 transport probe. Isolated test-only bridge; never imported by the console."""
import asyncio
import hmac
import json
import mimetypes
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit
from tornado import httpclient, httpserver, web, websocket


class State:
    def __init__(self, config):
        self.config = config
        self.launch = config['launch']
        self.cookie = secrets.token_hex(32)
        self.csrf = secrets.token_hex(32)
        self.style_nonce = secrets.token_hex(24)
        self.tickets = set()
        self.paths = []
        self.sockets = set()
        self.valid = True

    def authorized(self, handler):
        return self.valid and hmac.compare_digest(handler.get_cookie('sb_n01', ''), self.cookie)

    def trace(self, method, path, **details):
        self.paths.append(dict(method=method, path=path, **details))
        Path(self.config['trace']).write_text(json.dumps(self.paths, indent=2))


class Base(web.RequestHandler):
    @property
    def state(self):
        return self.settings['state']

    def set_default_headers(self):
        self.set_header('Cache-Control', 'no-store')
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.set_header('Content-Security-Policy', f"default-src 'none'; script-src 'self'; style-src 'self' 'nonce-{self.state.style_nonce}'; style-src-attr 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

    def prepare(self):
        expected = self.state.config['origin']
        if self.request.host != urlsplit(expected).netloc:
            raise web.HTTPError(403)
        origin = self.request.headers.get('Origin')
        if origin and origin != expected:
            raise web.HTTPError(403)
        if self.request.method not in ('GET', 'HEAD') and origin != expected:
            raise web.HTTPError(403)

    def require_session(self, csrf=False):
        if not self.state.authorized(self):
            raise web.HTTPError(401)
        if csrf and not hmac.compare_digest(self.request.headers.get('X-SB-CSRF', ''), self.state.csrf):
            raise web.HTTPError(403)


class Launch(Base):
    def get(self):
        self.require_session()
        self.write(dict(csrf=self.state.csrf))

    async def post(self):
        body = json.loads(self.request.body)
        if not self.state.launch or not hmac.compare_digest(body.get('launch', ''), self.state.launch):
            raise web.HTTPError(403)
        self.state.launch = None
        self.set_cookie('sb_n01', self.state.cookie, httponly=True, samesite='Strict', path='/')
        self.write(dict(csrf=self.state.csrf))


class Configuration(Base):
    def get(self):
        self.require_session(csrf=True)
        tickets = [secrets.token_hex(24) for _ in range(8)]
        self.state.tickets = set(tickets)
        self.write(dict(tickets=tickets, style_nonce=self.state.style_nonce))


class Logout(Base):
    def post(self):
        self.require_session(csrf=True)
        self.state.valid = False
        self.state.tickets.clear()
        for channel in list(self.state.sockets):
            channel.close(1008, 'Session revoked')
        self.finish()


class Asset(Base):
    def get(self, path=''):
        root = Path(self.state.config['assets'])
        target = (root / (path or 'index.html')).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise web.HTTPError(404)
        self.set_header('Content-Type', mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.write(target.read_bytes())


class Proxy(Base):
    async def forward(self, path):
        self.require_session(csrf=self.request.method != 'GET')
        if not re.fullmatch(r'api/(?:kernelspecs|kernels(?:/[a-z0-9-]+(?:/(?:interrupt|restart))?)?|sessions(?:/[a-z0-9-]+)?|contents(?:/[A-Za-z0-9_.-]+)?|me)', path):
            raise web.HTTPError(403)
        if path.startswith('api/contents/') and not path.endswith('.ipynb'):
            raise web.HTTPError(403)
        self.state.trace(self.request.method, path)
        upstream = self.state.config['upstream'] + '/jupyter/' + path
        if self.request.query:
            upstream += '?' + self.request.query
        headers = {'Authorization': 'token ' + self.state.config['jupyter_token'], 'Content-Type': 'application/json'}
        request = httpclient.HTTPRequest(upstream, method=self.request.method, headers=headers,
            body=self.request.body if self.request.method in ('POST','PUT','PATCH','DELETE') else None,
            allow_nonstandard_methods=True, request_timeout=180)
        response = await httpclient.AsyncHTTPClient().fetch(request, raise_error=False)
        self.set_status(response.code)
        self.set_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
        self.finish(None if response.code in (204,304) else response.body)
    get = post = put = patch = delete = forward


class Channel(websocket.WebSocketHandler):
    def initialize(self):
        self.upstream = None
        self.pump = None

    @property
    def state(self):
        return self.settings['state']

    def check_origin(self, origin):
        return origin == self.state.config['origin']

    async def prepare(self):
        if self.request.host != urlsplit(self.state.config['origin']).netloc or not self.state.authorized(self):
            raise web.HTTPError(403)
        if self.request.headers.get('Origin') != self.state.config['origin']:
            raise web.HTTPError(403)
        protocols = self.request.headers.get('Sec-WebSocket-Protocol', '').split(',')
        tickets = [p.strip()[8:] for p in protocols if p.strip().startswith('sb.auth.')]
        if len(tickets) != 1 or tickets[0] not in self.state.tickets:
            raise web.HTTPError(403)
        self.state.tickets.remove(tickets[0])

    def select_subprotocol(self, protocols):
        return 'v1.kernel.websocket.jupyter.org' if 'v1.kernel.websocket.jupyter.org' in protocols else None

    async def open(self, kernel_id):
        self.state.sockets.add(self)
        self.state.trace('WS', 'api/kernels/' + kernel_id + '/channels', protocol=self.selected_subprotocol)
        url = self.state.config['upstream'].replace('http:', 'ws:') + '/jupyter/api/kernels/' + kernel_id + '/channels'
        if self.request.query:
            url += '?' + self.request.query
        request = httpclient.HTTPRequest(url, headers={'Authorization': 'token ' + self.state.config['jupyter_token'], 'Origin': self.state.config['upstream']})
        self.upstream = await websocket.websocket_connect(request, subprotocols=[self.selected_subprotocol] if self.selected_subprotocol else [], max_message_size=2 * 1024 * 1024)
        async def relay():
            try:
                while (message := await self.upstream.read_message()) is not None:
                    await self.write_message(message, binary=isinstance(message, bytes))
            finally:
                self.close()
        self.pump = asyncio.create_task(relay())

    async def on_message(self, message):
        if self.upstream is None:
            self.close(1011, 'Upstream not ready')
            return
        await self.upstream.write_message(message, binary=isinstance(message, bytes))

    def on_close(self):
        self.state.sockets.discard(self)
        if self.upstream:
            self.upstream.close()
        if self.pump:
            self.pump.cancel()


async def serve(config):
    state = State(config)
    application = web.Application([(r'/launch', Launch), (r'/config', Configuration), (r'/logout', Logout),
        (r'/jupyter/api/kernels/([a-z0-9-]+)/channels', Channel), (r'/jupyter/(.*)', Proxy), (r'/(.*)', Asset)],
        state=state, websocket_max_message_size=2 * 1024 * 1024, log_function=lambda _: None)
    server = httpserver.HTTPServer(application, max_buffer_size=10 * 1024 * 1024)
    server.listen(config['bridge_port'], address='127.0.0.1')
    await asyncio.Event().wait()


if __name__ == '__main__':
    import sys
    asyncio.run(serve(json.loads(Path(sys.argv[1]).read_text())))
