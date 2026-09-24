#!/usr/bin/env python3
"""Real console transport survives a busy writer and fences changed bindings.

Uses a private source-mode daemon and minimal verified static assets; no engines,
user stack, browser, or external service is needed.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def qualify(binary):
    with tempfile.TemporaryDirectory(prefix='sb-health-',dir='/tmp') as tmp:
        root=Path(tmp).resolve();root.chmod(0o700)
        executable=root/'supabricks';shutil.copy2(binary,executable)
        data=root/'data';project=root/'project';project.mkdir()
        definition=str(uuid.uuid4());manifest=project/'supabricks.toml'
        manifest.write_text(f'format_version=1\nid="{definition}"\nname="health"\n')
        assets=root/'assets';assets.mkdir();html=b'<html><head></head><body>Console health regression</body></html>'
        (assets/'index.html').write_bytes(html)
        (assets/'console.json').write_text(json.dumps(dict(api_version=1,files={'index.html':hashlib.sha256(html).hexdigest()})))
        def rpc(**request):
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(10);s.connect(str(data/'control.sock'))
                s.sendall(json.dumps(dict(version=1,request=request)).encode()+b'\n')
                value=json.loads(s.makefile('rb').readline())
                assert 'error' not in value,value
                return value['result']
        def wait(call,timeout=15):
            until=time.monotonic()+timeout
            while time.monotonic()<until:
                result=call()
                if result:return result
                time.sleep(.1)
            raise AssertionError('console health condition timed out')
        with (root/'daemon.log').open('wb') as log:
            daemon=subprocess.Popen([str(executable),'daemon','--data-dir',str(data)],stdout=log,stderr=log)
            try:
                wait(lambda:(data/'control.sock').exists())
                context=rpc(method='resolve_binding',source=dict(definition_id=definition,worktree=str(project)))
                binding=dict(project_id=context['runtime_project_id'],worktree=str(project))
                def launch():
                    result=rpc(method='console_open',binding=binding,assets=str(assets))
                    return result if result['state']=='ready' else False
                launch=wait(launch);base,token=launch['url'].split('/#launch=')
                browser=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                def fetch(path,body=None):
                    request=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={
                        'x-supabricks-console':'1','Origin':base,'Content-Type':'application/json'})
                    try:
                        with browser.open(request,timeout=10) as response:return response.status,response.read()
                    except urllib.error.HTTPError as e:return e.code,e.read()
                assert fetch('/api/session',dict(token=token))[0]==200
                assert fetch('/api/overview')[0]==200
                os.kill(daemon.pid,signal.SIGSTOP)
                started=time.monotonic()
                try:
                    # Data remains unavailable while control is stalled. The
                    # static listener must survive more than two 6s deadlines.
                    assert fetch('/api/overview')[0]==503
                    time.sleep(max(0,16-(time.monotonic()-started)))
                    assert fetch('/')[0]==200
                finally:os.kill(daemon.pid,signal.SIGCONT)
                assert fetch('/api/overview')[0]==200
                print('PASS same console URL and session recover after a 16-second control stall',flush=True)
                manifest.write_text(f'format_version=1\nid="{uuid.uuid4()}"\nname="changed"\n')
                def closed():
                    try:
                        status,_=fetch('/api/overview');assert status!=200;return False
                    except (urllib.error.URLError,ConnectionResetError):return True
                wait(closed)
                print('PASS changed project binding terminates the console',flush=True)
            finally:
                if daemon.poll() is None:
                    os.kill(daemon.pid,signal.SIGCONT)
                    rpc(method='shutdown');daemon.wait(timeout=15)
                assert daemon.returncode==0


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--binary',type=Path,required=True)
    qualify(p.parse_args().binary.resolve())
