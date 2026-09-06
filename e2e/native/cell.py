#!/usr/bin/env python3
"""Qualify the actual daemon + Process Compose + PG17 + SeaweedFS cell.

Only this harness removes disposable engine state, in its own fresh test root.
It never removes object data or control state. SIGKILL is not power-loss testing.
"""
import argparse
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, BotoCoreError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def wait(action, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = action()
            if value:
                return value
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, ClientError, BotoCoreError) as error:
            last = str(error)
        time.sleep(0.2)
    raise RuntimeError(f"deadline exceeded: {last}")


def lsn(s):
    high, low = s.split('/')
    return int(high, 16) * 2**32 + int(low, 16)


class Cell:
    def __init__(self, binary, bundle, helpers, root, disk_full=False):
        self.binary = root / 'supabricks'
        shutil.copy2(binary, self.binary)
        self.bundle, self.helpers, self.root = bundle, helpers, root
        self.daemons = []
        self.checks = []
        self.disk_full = disk_full
        self.project = str(uuid.uuid4())
        self.env = dict(os.environ)
        for k in list(self.env):
            if k.startswith(('PG', 'AWS_', 'OTEL_', 'PC_')) or k.endswith('_PROXY'):
                self.env.pop(k)
        self.env['PGCONNECT_TIMEOUT'] = '2'

    def request(self, **request):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(20)
            client.connect(str(self.root / 'control.sock'))
            client.sendall(json.dumps(dict(version=1, request=request)).encode() + b'\n')
            response = json.loads(client.makefile('rb').readline())
        if 'error' in response:
            raise RuntimeError(str(response['error']))
        return response['result']

    def start(self):
        log = (self.root / 'daemon.log').open('ab')
        p = subprocess.Popen([str(self.binary), 'daemon', '--data-dir', str(self.root),
                              '--bundle', str(self.bundle), '--helpers', str(self.helpers)],
                             env=self.env, stdin=subprocess.DEVNULL, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.daemons.append(p)
        def ready():
            if p.poll() is not None:
                raise AssertionError('daemon exited during startup; inspect daemon.log')
            return self.request(method='status')['engine_execution']
        wait(ready)
        self.config = json.loads((self.root / 'runtime.json').read_text())
        self.s3 = boto3.client('s3', endpoint_url=f"http://127.0.0.1:{self.config['ports']['weed_s3']}",
                              aws_access_key_id=self.config['s3_access'],
                              aws_secret_access_key=self.config['s3_secret'], region_name='us-east-1',
                              config=Config(signature_version='s3v4', retries={'max_attempts': 0},
                                            connect_timeout=2, read_timeout=10,
                                            s3={'addressing_style': 'path'},
                                            request_checksum_calculation='when_required',
                                            response_checksum_validation='when_required'))
        wait(lambda: self.s3.head_bucket(Bucket='supabricks'))
        return p

    def records(self):
        with sqlite3.connect(f'file:{self.root}/state.sqlite3?mode=ro', uri=True) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT record_json FROM native_processes')]

    def credentials(self, branch):
        endpoint = branch['endpoint']['id']
        with sqlite3.connect(f'file:{self.root}/state.sqlite3?mode=ro', uri=True) as db:
            return db.execute('SELECT password FROM credentials WHERE endpoint_id=?', (endpoint,)).fetchone()[0]

    def sql(self, branch, query, password=None):
        env = dict(self.env, PGPASSWORD=self.credentials(branch) if password is None else password)
        p = subprocess.run([str(self.bundle / 'pg_install/v17/bin/psql'), '-XAt', '-v', 'ON_ERROR_STOP=1',
                            '-h', '127.0.0.1', '-p', str(branch['ports']['sql']), '-U', 'cloud_admin',
                            '-d', 'postgres', '-c', query], env=env, capture_output=True, text=True, timeout=30)
        if p.returncode:
            raise RuntimeError(p.stderr.strip())
        return p.stdout.strip()

    def create(self, name):
        listeners = [socket.socket() for _ in range(3)]
        try:
            for listener in listeners:
                listener.bind(('127.0.0.1', 0))
            ports = dict(zip(['sql', 'external_http', 'internal_http'],
                             [s.getsockname()[1] for s in listeners]))
        finally:
            for listener in listeners:
                listener.close()
        op = self.request(method='submit', project_id=self.project, key=f'create-{name}',
                          mutation=dict(kind='create_branch', name=name, parent_id=None, ports=ports))
        self.operation(op)
        branch = self.request(method='branch', id=op['branch_id'])
        wait(lambda: self.sql(branch, 'SELECT 1') == '1')
        return branch

    def operation(self, op):
        return wait(lambda: self.request(method='operation', id=op['id'])['status'] == 'succeeded')

    def state(self, branch, desired):
        branch = self.request(method='branch', id=branch['branch']['id'])
        op = self.request(method='submit', project_id=self.project, key=str(uuid.uuid4()),
                          mutation=dict(kind='set_state', branch_id=branch['branch']['id'],
                                        expected_revision=branch['revision'], desired=desired))
        self.operation(op)
        return self.request(method='branch', id=branch['branch']['id'])

    def ps(self, branch):
        der = (self.root / 'storage.pk8').read_bytes()
        offset = der.index(bytes.fromhex('04220420')) + 4
        key = Ed25519PrivateKey.from_private_bytes(der[offset:offset+32])
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
        message = b64(b'{"alg":"EdDSA"}') + b'.' + b64(b'{"scope":"pageserverapi"}')
        token = (message + b'.' + b64(key.sign(message))).decode()
        path = f"tenant/{branch['branch']['tenant_id']}/timeline/{branch['branch']['timeline_id']}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.config['ports']['ps_http']}/v1/{path}",
                                     headers={'Authorization': 'Bearer ' + token})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)

    def stop(self):
        self.request(method='shutdown')
        wait(lambda: not (self.root / 'control.sock').exists(), timeout=60)
        for p in self.daemons:
            if p.poll() is None:
                p.wait(timeout=10)
        assert not self.records(), 'owned processes remain after shutdown'

    def s3_operations(self):
        b = 'supabricks'
        content = bytes(range(256)) * 8192
        self.s3.put_object(Bucket=b, Key='qualification/range', Body=content)
        assert self.s3.get_object(Bucket=b, Key='qualification/range', Range='bytes=1021-4097')['Body'].read() == content[1021:4098]
        assert self.s3.head_object(Bucket=b, Key='qualification/range')['ContentLength'] == len(content)
        upload = self.s3.create_multipart_upload(Bucket=b, Key='qualification/multipart')['UploadId']
        parts, expected = [], b''
        for number, data in enumerate([b'a' * (5 * 1024**2), b'b' * 12345], 1):
            etag = self.s3.upload_part(Bucket=b, Key='qualification/multipart', UploadId=upload,
                                       PartNumber=number, Body=data)['ETag']
            parts.append(dict(ETag=etag, PartNumber=number)); expected += data
        self.s3.complete_multipart_upload(Bucket=b, Key='qualification/multipart', UploadId=upload,
                                         MultipartUpload={'Parts': parts})
        assert self.s3.get_object(Bucket=b, Key='qualification/multipart')['Body'].read() == expected
        upload = self.s3.create_multipart_upload(Bucket=b, Key='qualification/aborted')['UploadId']
        self.s3.abort_multipart_upload(Bucket=b, Key='qualification/aborted', UploadId=upload)
        listed = list(self.s3.get_paginator('list_objects_v2').paginate(Bucket=b, Prefix='qualification/', PaginationConfig={'PageSize': 1}))
        assert sum(len(p.get('Contents', [])) for p in listed) == 2
        self.s3.delete_objects(Bucket=b, Delete={'Objects': [{'Key': 'qualification/range'}, {'Key': 'qualification/multipart'}]})
        assert not self.s3.list_objects_v2(Bucket=b, Prefix='qualification/').get('Contents')
        self.s3.put_object(Bucket=b, Key='qualification/restart', Body=b'acknowledged-object')
        self.checks.append('S3 authenticated PUT, HEAD, range GET, paginated LIST, multipart complete/abort, batch DELETE')

    def exhaust_space(self, branch):
        # The caller mounted a small disposable tmpfs here. Never fill the host
        # filesystem: refuse the test unless this is an independent mount.
        objects = self.root / 'objects'
        assert os.path.ismount(objects)
        filler = objects / 'test-only-space-filler'
        try:
            with filler.open('wb', buffering=0) as f:
                try:
                    while True:
                        f.write(b'\0' * (1024 * 1024))
                except OSError as e:
                    assert e.errno == errno.ENOSPC
            try:
                self.s3.put_object(Bucket='supabricks', Key='qualification/must-not-ack', Body=os.urandom(1024 * 1024))
                raise AssertionError('S3 acknowledged a new object on the full filesystem')
            except (ClientError, BotoCoreError):
                pass
        finally:
            filler.unlink(missing_ok=True)
        self.stop(); self.start()
        wait(lambda: self.sql(branch, 'SELECT count(*),sum(amount) FROM orders') == '1000|625625.00')
        assert self.s3.get_object(Bucket='supabricks', Key='qualification/restart')['Body'].read() == b'acknowledged-object'
        self.checks.append('bounded filesystem ENOSPC refuses S3 writes; prior acknowledged SQL and objects survive restart')

    def exercise(self):
        self.start()
        # The private supervisor and S3 must reject unauthenticated clients.
        for port, path in [('supervisor', '/processes'), ('weed_s3', '/supabricks')]:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.config['ports'][port]}{path}", timeout=3)
                raise AssertionError(f'{port} accepts unauthenticated requests')
            except urllib.error.HTTPError as e:
                assert e.code in (401, 403)
        self.s3_operations()
        self.request(method='register_project', config=dict(format_version=1, id=self.project, name='native-test'))
        main = self.create('main')
        assert self.sql(main, 'SHOW server_version_num') == '170008'
        try:
            self.sql(main, 'SELECT 1', password='incorrect')
            raise AssertionError('SQL accepts incorrect credentials')
        except RuntimeError as e:
            assert 'authentication failed' in str(e)
        self.sql(main, 'CREATE TABLE orders(id int PRIMARY KEY, amount numeric(12,2)); INSERT INTO orders SELECT i, i*1.25 FROM generate_series(1,1000)i;')
        expected = '1000|625625.00'
        assert self.sql(main, 'SELECT count(*),sum(amount) FROM orders') == expected
        self.checks.append('PG17.8 SQL, enforced password authentication, acknowledged writes')
        if self.disk_full:
            self.exhaust_space(main)
        storage = {r['role']: r['pid'] for r in self.records() if r['branch'] is None}
        other = self.create('other')
        other = self.state(other, 'suspended')
        time.sleep(1)
        assert not any(r['branch'] and r['branch'][0] == other['branch']['id'] for r in self.records())
        other = self.state(other, 'running')
        self.state(other, 'deleted')
        assert storage == {r['role']: r['pid'] for r in self.records() if r['branch'] is None}
        self.checks.append('dynamic compute add, suspend, wake and delete leave storage PIDs unchanged')
        # Kill compute_ctl alone: its orphaned Postgres must be stopped before replacement.
        compute = next(r for r in self.records() if r['branch'])
        os.kill(compute['pid'], signal.SIGKILL)
        wait(lambda: any(r['branch'] and r['pid'] != compute['pid'] for r in self.records()))
        wait(lambda: self.sql(main, 'SELECT count(*),sum(amount) FROM orders') == expected)
        self.checks.append('orphaned Postgres descendants fenced after compute_ctl SIGKILL')
        for victim in ['supervisor', 'daemon']:
            before = self.records()
            if victim == 'supervisor':
                os.kill(next(r['pid'] for r in before if r['role'] == victim), signal.SIGKILL)
            else:
                self.daemons[-1].kill(); self.daemons[-1].wait(timeout=5)
                self.start()
            wait(lambda: all(r['pid'] not in {old['pid'] for old in before} for r in self.records()) and len(self.records()) >= 6)
            wait(lambda: self.sql(main, 'SELECT count(*),sum(amount) FROM orders') == expected)
            assert len([r for r in self.records() if r['branch']]) == 1
            groups = subprocess.check_output(['ps','-axo','pgid=,stat='],text=True)
            live = {int(line.split()[0]) for line in groups.splitlines() if not line.split()[1].startswith('Z')}
            assert not (live & {r['pid'] for r in before}), 'old writable process group survived recovery'
            self.checks.append(f'{victim} SIGKILL recovery preserves SQL and replaces owned processes')
        point = self.sql(main, 'SELECT pg_current_wal_flush_lsn()')
        wait(lambda: lsn(self.ps(main)['remote_consistent_lsn']) >= lsn(point))
        remote = self.s3.list_objects_v2(Bucket='supabricks', Prefix='pageserver/')['Contents']
        assert any('index_part' in obj['Key'] for obj in remote)
        self.stop()
        # Actual S3 recovery: retain objects/ and state.sqlite3; delete ONLY
        # disposable engine state under this test's fresh, uniquely named root.
        assert self.root.name.startswith('sb-p03-')
        for name in ['pageserver/tenants', 'safekeeper', 'computes']:
            shutil.rmtree(self.root / name)
        self.start()
        wait(lambda: self.sql(main, 'SELECT count(*),sum(amount) FROM orders') == expected)
        assert self.s3.get_object(Bucket='supabricks', Key='qualification/restart')['Body'].read() == b'acknowledged-object'
        self.checks.append('cold restore from SeaweedFS after removing pageserver tenants, safekeeper WAL and PGDATA')
        self.stop()
        self.checks.append('all recorded children stopped; private control files retained')
        return dict(status='PASS', checks=self.checks, limits=['SIGKILL/restart evidence, not power-loss qualification', 'single safekeeper and single-owner host', 'engineering artifacts; no public installer'])

    def close(self):
        try:
            if (self.root / 'control.sock').exists():
                self.stop()
        except Exception:
            for p in self.daemons:
                if p.poll() is None:
                    p.kill(); p.wait(timeout=10)
            # Test-owned root, but use the runtime's ownership verification for cleanup.
            try:
                self.start(); self.stop()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--helpers', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--disk-full', action='store_true', help='Linux CI only: mount a bounded tmpfs for actual ENOSPC injection')
    args = parser.parse_args()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    root = Path(tempfile.mkdtemp(prefix='sb-p03-', dir='/tmp')).resolve()
    if args.disk_full:
        assert os.uname().sysname == 'Linux'
        (root/'objects').mkdir(mode=0o700)
        subprocess.run(['sudo','-n','mount','-t','tmpfs','-o',f'size=384m,uid={os.getuid()},gid={os.getgid()},mode=0700','tmpfs',str(root/'objects')],check=True)
    cell = Cell(args.binary.resolve(), args.bundle.resolve(), args.helpers.resolve(), root, args.disk_full)
    try:
        report = cell.exercise()
        args.report.write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2), flush=True)
    except BaseException:
        print(f'Failed native cell retained at {root}', flush=True)
        print('Completed checks:', cell.checks, flush=True)
        raise
    finally:
        cell.close()
        if args.disk_full:
            subprocess.run(['sudo','-n','umount',str(root/'objects')],check=True)


if __name__ == '__main__':
    main()
