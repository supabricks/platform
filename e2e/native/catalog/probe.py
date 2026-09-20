#!/usr/bin/env python3
"""UC00: isolated, real PG -> Delta -> UC -> source-built Sail qualification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from epochs import Epochs
from server import Server, clean_env

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()


class Sail:
    def __init__(self, python, server, token, root, evidence, storage=None):
        self.evidence = evidence
        self.worker_id = str(uuid.uuid4())
        self.log = (root/f'sail-{uuid.uuid4()}.log').open('w')
        env = clean_env()
        env['AWS_EC2_METADATA_DISABLED'] = 'true'
        env['AWS_CONFIG_FILE'] = str(root/'absent-aws-config')
        env['AWS_SHARED_CREDENTIALS_FILE'] = str(root/'absent-aws-credentials')
        self.proc = subprocess.Popen([str(python), str(HERE/'sail_worker.py')], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, bufsize=1)
        self.results = queue.Queue()
        def read():
            for line in self.proc.stdout:
                if line.startswith('UC00_RESULT '): self.results.put(json.loads(line[12:]))
            self.results.put(None)
        self.thread = threading.Thread(target=read, daemon=True)
        self.thread.start()
        self.proc.stdin.write(json.dumps(dict(uri=server.base+'/api/2.1/unity-catalog',
            token=token, storage=storage or {}))+'\n')
        self.proc.stdin.flush()

    def query(self, sql):
        self.proc.stdin.write(json.dumps(dict(sql=sql))+'\n'); self.proc.stdin.flush()
        result = self.results.get(timeout=90)
        assert result is not None, 'Sail exited; inspect private worker log'
        self.evidence.append(dict(worker_id=self.worker_id, sql=sql, result=result))
        return result

    def rows(self, sql):
        result = self.query(sql)
        assert result['ok'], result
        return result['rows']

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            self.proc.kill(); self.proc.wait(timeout=5)
        self.thread.join(timeout=2)
        self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('release', 'uc-runtime', 'report'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--keep-state', action='store_true')
    args = parser.parse_args()
    release, runtime = args.release.resolve(), args.uc_runtime.resolve()
    root = Path(tempfile.mkdtemp(prefix='sb-uc00-', dir='/tmp')).resolve()
    os.chmod(root, 0o700)
    print('Private probe state: '+str(root), flush=True)
    checks = []
    manifest = json.loads((release/'release.json').read_text())
    for name, entry in manifest['files'].items():
        file = release/name
        assert file.resolve().is_relative_to(release), name
        assert sha(file) == entry['sha256'], 'baseline file differs from inventory: '+name
    if os.environ.get('SB_UC00_NETWORK_EVIDENCE'):
        try:
            with socket.create_connection(('1.1.1.1',443),timeout=2): pass
        except OSError: pass
        else: raise AssertionError('offline qualification has external TCP access')
    report = dict(schema_version=1, status='FAIL', target=platform.system()+'-'+platform.machine(),
        network_evidence=os.environ.get('SB_UC00_NETWORK_EVIDENCE','local run; external network not isolated'),
        uc_build=json.loads((runtime/'build.json').read_text()),
        uc_artifact=json.loads(runtime.with_suffix('.artifact.json').read_text()),
        sail_build=json.loads((release/'provenance/sail/sail-build.json').read_text()),
        platform_binary_sha256=sha(release/'bin/supabricks'), release_inventory_sha256=sha(release/'release.json'), checks=checks, queries=[])
    pin = json.loads((REPO/'components/sail-source.lock.json').read_text())
    assert report['sail_build']['commit'] == pin['commit']
    uc_pin = json.loads((REPO/'components/unity-catalog-source.lock.json').read_text())
    assert report['uc_build']['source_commit'] == uc_pin['commit']
    assert report['uc_build']['source_pin_sha256'] == sha(REPO/'components/unity-catalog-source.lock.json')
    for entry in report['uc_build']['jars']:
        assert sha(runtime/entry['path']) == entry['sha256'], entry['path']
    cellroot = root/'cell'; cellroot.mkdir(mode=0o700)
    cell = Epochs(release/'bin/supabricks', release/'engine', release/'helpers', cellroot)
    cell.python = release/'python/analytics/python'
    cell.work = root/'producer'; cell.work.mkdir()
    (cell.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{cell.project}"\nname = "producer"\n')
    server = None
    workers = []
    def check(name, **facts):
        checks.append(dict(name=name, status='PASS', **facts)); print('PASS '+name, flush=True)
    def worker(token, storage=None):
        result = Sail(cell.python, server, token, root, report['queries'], storage); workers.append(result); return result
    def register(catalog, schema, name, location, epoch):
        return server.ok('POST', 'tables', dict(name=name, catalog_name=catalog, schema_name=schema,
            table_type='EXTERNAL', data_source_format='DELTA', storage_location=location,
            properties={'supabricks.probe.epoch': epoch}, columns=[dict(name=n, type_name='INT',
                type_text='int', type_json=json.dumps(dict(name=n,type='integer',nullable=True,metadata={})),
                nullable=True, position=i) for i,n in enumerate(('id','amount'))]))
    try:
        cell.start()
        cell.request(method='resolve_binding', source=dict(definition_id=cell.project, worktree=str(cell.work)))
        second_id = str(uuid.uuid4()); second_work = root/'consumer'; second_work.mkdir()
        (second_work/'supabricks.toml').write_text(f'format_version = 1\nid = "{second_id}"\nname = "consumer"\n')
        cell.request(method='resolve_binding', source=dict(definition_id=second_id, worktree=str(second_work)))
        branch = cell.create('main')
        cell.configure(release/'python/analytics/export.py')
        cell.sql(branch, 'CREATE TABLE orders(id int,amount int); CREATE TABLE payments(id int,amount int); INSERT INTO orders VALUES(1,10); INSERT INTO payments SELECT * FROM orders')
        _, first = cell.publish()
        cell.cli('analytics','pin',first['epoch_id'],'--ttl-ms','3600000')
        cell.sql(branch, 'BEGIN; INSERT INTO orders VALUES(2,20); INSERT INTO payments VALUES(2,20); COMMIT')
        _, second = cell.publish()
        cell.cli('analytics','pin',second['epoch_id'],'--ttl-ms','3600000')
        def location(publication, table):
            descriptor = publication['descriptor']
            entry = next(t for t in descriptor['manifest']['tables'] if t['name'] == table)
            assert entry['version'] == 0, entry
            return (cell.root/descriptor['generation']/entry['path']).as_uri()
        check('two_projects_and_two_complete_pg_export_epochs')
        server = Server(runtime, root/'uc', cell.config)
        server.start()
        reader1, reader2 = 'producer@example.test', 'consumer@example.test'
        for email in (reader1, reader2): server.user(email)
        for catalog in ('p1','p2'):
            server.ok('POST','catalogs',dict(name=catalog))
            for schema in ('current','epoch1','epoch2'):
                server.ok('POST','schemas',dict(name=schema,catalog_name=catalog))
        for table in ('orders','payments'):
            register('p1', 'current', table, location(first, table), first['epoch_id'])
        # UC prohibits overlapping registered table locations, even across
        # catalogs. A consumer binding must reference the canonical table ID.
        overlap = server.request('POST','tables',dict(name='duplicate',catalog_name='p2',schema_name='current',
            table_type='EXTERNAL',data_source_format='DELTA',storage_location=location(first,'orders'),columns=[]))[0]
        assert overlap in (400,403,409), overlap
        from urllib.parse import urlparse, unquote
        private_path = root/'consumer-data'
        shutil.copytree(Path(unquote(urlparse(location(first,'orders')).path)),private_path)
        register('p2','current','private',private_path.as_uri(),first['epoch_id'])
        check('duplicate_registration_at_same_location_rejected', http_status=overlap)
        for catalog, email in (('p1',reader1), ('p2',reader2)):
            server.grant('catalog',catalog,email,['USE CATALOG'])
            for schema in ('current','epoch1','epoch2'):
                server.grant('schema',catalog+'.'+schema,email,['USE SCHEMA'])
        for schema in ('current',):
            for table in ('orders','payments'):
                server.grant('table',f'p1.{schema}.{table}',reader1,['SELECT'])
        server.grant('table','p2.current.private',reader2,['SELECT'])
        t1, t2 = server.token(reader1), server.token(reader2)
        table = server.ok('GET','tables/p1.current.orders',token=t1)
        denied = server.request('GET','tables/p1.current.orders',token=t2)[0]
        assert denied in (401,403,404), denied
        expired = server.request('GET','catalogs',token=server.token(reader1,-60))[0]
        assert expired == 401, expired
        check('metadata_permissions_and_expired_token', denied_http=denied, expired_http=expired)
        owner, consumer = worker(t1), worker(t2)
        assert owner.rows('SELECT sum(amount) AS total FROM p1.current.orders') == [dict(total=10)]
        assert consumer.rows('SELECT sum(amount) AS total FROM p2.current.private') == [dict(total=10)]
        assert not consumer.query('SELECT * FROM p1.current.orders')['ok']
        assert not worker(server.token(reader1,-60)).query('SELECT * FROM p1.current.orders')['ok']
        listed = owner.rows('SHOW TABLES IN p1.current')
        assert {r['tableName'] for r in listed} == {'orders','payments'}, listed
        described = owner.rows('DESCRIBE TABLE p1.current.orders')
        assert any(r.get('col_name') == 'amount' for r in described), described
        check('native_sail_catalog_reads_and_cross_principal_denial')
        # Local OS ownership can read bytes without consulting UC. This is an
        # expected capability limit, never a claim of governed data isolation.
        bypass = consumer.rows(f"SELECT sum(amount) AS total FROM delta.`{location(first,'orders')}`")
        assert bypass == [dict(total=10)]
        check('local_owner_direct_path_bypasses_catalog', governed_local_storage=False)
        server.grant('table','p1.current.orders',reader1,remove=['SELECT'])
        assert not owner.query('SELECT * FROM p1.current.orders')['ok']
        server.grant('table','p1.current.orders',reader1,['SELECT'])
        check('revocation_in_existing_uncached_sail_session')
        # Resolve both authorized locations once, then pin their Delta versions.
        # This is a probe of the UC04 adapter strategy, not a product feature.
        for name in ('orders','payments'):
            resolved = server.ok('GET','tables/p1.current.'+name,token=t1)
            assert resolved['storage_location'] == location(first,name)
            owner.rows(f"CREATE TEMPORARY VIEW frozen_{name} AS SELECT * FROM delta.`{resolved['storage_location']}` VERSION AS OF 0")
        # Name reuse must not imply identity or inherit old grants.
        server.ok('DELETE','tables/p1.current.orders')
        recreated = register('p1','current','orders',location(second,'orders'),second['epoch_id'])
        assert recreated['table_id'] != table['table_id']
        assert server.request('GET','tables/p1.current.orders',token=t1)[0] in (403,404)
        server.grant('table','p1.current.orders',reader1,['SELECT'])
        assert owner.rows('SELECT sum(amount) AS total FROM p1.current.orders') == [dict(total=30)]
        # An ordinary multi-table query can now see a mixed snapshot set.
        totals = owner.rows('SELECT (SELECT sum(amount) FROM p1.current.orders) AS orders, (SELECT sum(amount) FROM p1.current.payments) AS payments')
        assert totals == [dict(orders=30,payments=10)], totals
        frozen = owner.rows('SELECT (SELECT sum(amount) FROM frozen_orders) AS orders, (SELECT sum(amount) FROM frozen_payments) AS payments')
        assert frozen == [dict(orders=10,payments=10)]
        check('recreate_changes_identity_and_mutable_names_mix_epochs', frozen_namespace_consistent=True)
        rename = server.request('PATCH','tables/p1.current.orders',dict(new_name='renamed'))[0]
        assert rename in (404,405,500,501), rename
        assert server.ok('GET','tables/p1.current.orders')['table_id'] == recreated['table_id']
        assert server.request('GET','tables/p1.current.renamed')[0] == 404
        check('table_rename_unavailable', http_status=rename)
        # Use an existing local object for the actual file credential probe.
        local_table = server.ok('GET','tables/p1.current.payments')
        local_status, local_credentials = server.request('POST','temporary-table-credentials',dict(table_id=local_table['table_id'],operation='READ'),token=t1)
        assert local_status == 200
        assert not any(local_credentials.get(k) for k in ('aws_temp_credentials','azure_user_delegation_sas','gcp_oauth_token'))
        check('local_file_credential_vending_observed', http_status=local_status, storage_credentials=False)
        # Upload the same immutable Delta tree into the cell's existing SeaweedFS.
        from urllib.parse import urlparse, unquote
        source = Path(unquote(urlparse(location(first,'orders')).path))
        prefix = 'uc00/'+first['epoch_id']+'/orders'
        for file in source.rglob('*'):
            if file.is_file(): cell.s3.upload_file(str(file),'supabricks',prefix+'/'+file.relative_to(source).as_posix())
        remote = register('p1','current','s3orders','s3://supabricks/'+prefix,first['epoch_id'])
        server.grant('table','p1.current.s3orders',reader1,['SELECT'])
        status, creds = server.request('POST','temporary-table-credentials',dict(table_id=remote['table_id'],operation='READ'),token=t1)
        assert status == 200, status
        assert creds['aws_temp_credentials']['access_key_id'] == cell.config['s3_access']
        check('seaweed_static_test_credentials_vended', scoped_sts=False)
        storage = dict(AWS_ENDPOINT=f"http://127.0.0.1:{cell.config['ports']['weed_s3']}", AWS_ALLOW_HTTP='true',
            AWS_REGION='us-east-1', AWS_VIRTUAL_HOSTED_STYLE_REQUEST='false')
        assert not worker(t1,storage).query('SELECT * FROM p1.current.s3orders')['ok']
        storage.update(AWS_ACCESS_KEY_ID=cell.config['s3_access'],AWS_SECRET_ACCESS_KEY=cell.config['s3_secret'])
        assert worker(t1,storage).rows('SELECT sum(amount) AS total FROM p1.current.s3orders') == [dict(total=10)]
        check('sail_seaweed_read_needs_explicit_storage_credentials', native_vending=False, path_style=True)
        server.stop()
        assert not owner.query('SELECT * FROM p1.current.orders')['ok']
        backup = root/'stopped-backup'; shutil.copytree(server.root/'etc',backup)
        server.start()
        assert server.ok('GET','tables/p1.current.orders')['table_id'] == recreated['table_id']
        assert owner.rows('SELECT sum(amount) AS total FROM p1.current.orders') == [dict(total=30)]
        server.stop()
        shutil.rmtree(server.root/'etc'); shutil.copytree(backup,server.root/'etc')
        server.start()
        assert server.ok('GET','tables/p1.current.orders')['table_id'] == recreated['table_id']
        assert server.request('GET','tables/p1.current.orders',token=t2)[0] in (403,404)
        assert owner.rows('SELECT sum(amount) AS total FROM frozen_orders') == [dict(total=10)]
        check('outage_restart_and_stopped_metadata_restore', backend='H2', data_backup_separate=True)
        short_lived = worker(server.token(reader1, 8))
        assert short_lived.rows('SELECT sum(amount) AS total FROM p1.current.orders') == [dict(total=30)]
        time.sleep(9)
        assert not short_lived.query('SELECT * FROM p1.current.orders')['ok']
        check('expiry_in_existing_uncached_sail_session')
        report['metrics'] = dict(readiness_seconds=server.ready_times, idle_rss_bytes=max(server.idle_samples),
            peak_rss_bytes=server.peak, compressed_runtime_bytes=report['uc_artifact']['compressed_bytes'])
        assert max(server.ready_times)<20, report['metrics']
        assert max(server.idle_samples)<512*1024*1024, report['metrics']
        assert report['uc_artifact']['compressed_bytes']<300*1024*1024, report['metrics']
        check('local_footprint_budgets')
        report['status'] = 'PASS'
    finally:
        for w in workers: w.close()
        if server: server.stop()
        cell.close()
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS' and not args.keep_state: shutil.rmtree(root)
    print(json.dumps(dict(status=report['status'], checks=len(checks), metrics=report.get('metrics')),indent=2))


if __name__ == '__main__': main()
