#!/usr/bin/env python3
"""UC01 installed service fixture: current binary/UC plus pinned PG/Sail baseline.

This checks installed discovery and lifecycle without claiming a complete new
release qualification. native-release separately assembles the entire candidate.
"""
import argparse
from datetime import datetime, timedelta, timezone
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from probe import CatalogCell, sha
from server import Server
from cell import wait

REPO=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(REPO/'install/native'))
from unity_catalog import install


def installed_fixture(baseline, binary, runtime, output):
    manifest=json.loads((baseline/'release.json').read_text())
    for name,entry in manifest['files'].items():
        assert (baseline/name).resolve().is_relative_to(baseline)
        assert sha(baseline/name)==entry['sha256'],name
    shutil.copytree(baseline,output)
    shutil.copy2(binary,output/'bin/supabricks')
    catalog=install(output,manifest['target'],runtime)
    manifest['provenance']['unity_catalog']=catalog
    manifest['provenance']['data_formats']['unity_catalog']=1
    manifest['provenance']['uc01_fixture']='current platform binary and UC closure over immutable alpha.24 engines; not full release qualification'
    manifest['files']={str(p.relative_to(output)):dict(sha256=sha(p),executable=bool(p.stat().st_mode&0o111)) for p in sorted(output.rglob('*')) if p.is_file() and p!=output/'release.json'}
    (output/'release.json').write_text(json.dumps(manifest,indent=2)+'\n')
    subprocess.run([str(output/'bin/supabricks'),'installation','verify'],check=True,capture_output=True)


def api(endpoint, token, path='catalogs', method='GET', body=None):
    request=urllib.request.Request(endpoint+'/api/2.1/unity-catalog/'+path,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method=method,
        data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request,timeout=3) as response:
            raw=response.read();return response.status,json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:return error.code,None


def tls_proxy(root, upstream):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    issuer=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'UC01 test CA')])
    ca_certificate=(x509.CertificateBuilder().subject_name(issuer).issuer_name(issuer).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
        .sign(ca_key,hashes.SHA256()))
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'UC01 test server')])
    certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
        .sign(ca_key,hashes.SHA256()))
    ca=root/'ca.pem';ca.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM));ca.chmod(0o600)
    cert=root/'tls.pem';cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM));cert.chmod(0o600)
    private=root/'tls.key';private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));private.chmod(0o600)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            request=urllib.request.Request(upstream+self.path,headers={'Authorization':self.headers.get('Authorization','')})
            try:
                with urllib.request.urlopen(request,timeout=3) as response:status,body=response.status,response.read()
            except urllib.error.HTTPError as error:status,body=error.code,error.read()
            self.send_response(status);self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,private)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    return server,thread,ca


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['release','binary','uc-runtime','report']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='sb-uc01-',dir='/tmp')).resolve();root.chmod(0o700)
    print('Private service fixture:',root,flush=True)
    baseline,binary,runtime=args.release.resolve(),args.binary.resolve(),args.uc_runtime.resolve()
    report=dict(status='FAIL',checks=[],binary_sha256=sha(binary),uc_build=json.loads((runtime/'build.json').read_text()),
        network_evidence=os.environ.get('SB_UC00_NETWORK_EVIDENCE','local development; external network not isolated'))
    if os.environ.get('SB_UC00_NETWORK_EVIDENCE'):
        try:
            with socket.create_connection(('1.1.1.1',443),timeout=2):pass
        except OSError as error:
            assert error.errno in (errno.EPERM,errno.EACCES,errno.ENETUNREACH)
            report['external_tcp_denial']=errno.errorcode[error.errno]
        else:raise AssertionError('offline fixture has external TCP access')
    installed=root/'release';installed_fixture(baseline,binary,runtime,installed)
    cellroot=root/'data';cellroot.mkdir(mode=0o700)
    cell=CatalogCell(installed/'bin/supabricks',installed/'engine',installed/'helpers',cellroot)
    cell.binary=installed/'bin/supabricks'  # exercise installed discovery, not a copied development binary
    external=proxy=thread=None
    def check(name,**facts):report['checks'].append(dict(name=name,**facts));print('PASS',name,flush=True)
    def status():return cell.request(method='status')['catalog']
    def ready():return wait(lambda: (s if (s:=status())['ready'] else None),timeout=65)
    def command(action,**args):return cell.request(method='catalog_service',command=dict(action=action,**args))
    def owned():return next(r for r in cell.records() if r['role']=='unity-catalog')
    def token():return (cellroot/'catalog/etc/conf/token.txt').read_text().strip()
    try:
        cell.start();first=ready()
        process=psutil.Process(owned()['pid'])
        listeners=[c for c in process.net_connections(kind='inet') if c.status=='LISTEN']
        assert len(listeners)>=2
        assert all((getattr(ipaddress.ip_address(c.laddr.ip),'ipv4_mapped',None) or ipaddress.ip_address(c.laddr.ip)).is_loopback for c in listeners)
        assert process.exe()==str((installed/'share/unity-catalog/java/bin/java').resolve())
        assert first['readiness_seconds']<20
        assert process.memory_info().rss<512*1024*1024
        check('installed_private_jre_authenticated_loopback_bootstrap',readiness_seconds=first['readiness_seconds'],idle_rss_bytes=process.memory_info().rss)
        for name in ['public_key.der','private_key.der','key_id.txt','token.txt']:
            assert (cellroot/'catalog/etc/conf'/name).stat().st_mode&0o077==0
        assert token() not in json.dumps(cell.request(method='status'))
        assert token() not in ' '.join(process.cmdline())
        assert api(first['endpoint'],'invalid-token')[0]==401
        check('private_keys_and_credentials_absent_from_status_and_argv')
        code,entry=api(first['endpoint'],token(),method='POST',body=dict(name='uc01_recovery'))
        assert code in (200,201), ('create_catalog_status',code)
        catalog_id=entry['id']
        work=root/'project';work.mkdir();(work/'supabricks.toml').write_text(f'format_version=1\nid="{cell.project}"\nname="uc01"\n')
        cell.request(method='resolve_binding',source=dict(definition_id=cell.project,worktree=str(work)))
        branch=cell.create('main');assert cell.sql(branch,'SELECT 42')=='42'
        old_token=token();command('rotate_key');rotated=ready()
        old_status=api(rotated['endpoint'],old_token)[0]
        assert old_status in (401,403), ('old_key_token_status',old_status)
        assert api(rotated['endpoint'],token(),'catalogs/uc01_recovery')[1]['id']==catalog_id
        assert rotated['metastore_id']==first['metastore_id'] and rotated['provider_id']==first['provider_id']
        check('key_rotation_revokes_old_token_preserves_metadata_and_identity')
        previous_port=int(rotated['endpoint'].rsplit(':',1)[1])
        victim=psutil.Process(owned()['pid']);os.kill(victim.pid,signal.SIGKILL)
        wait(lambda:not victim.is_running() or victim.status()==psutil.STATUS_ZOMBIE)
        blocker=socket.socket();blocker.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        blocker.bind(('127.0.0.1',previous_port));blocker.listen()
        try:
            assert cell.sql(branch,'SELECT 42')=='42'
            recovered=wait(lambda:(s if (s:=status())['ready'] and s['endpoint']!=rotated['endpoint'] else None))
            assert int(recovered['endpoint'].rsplit(':',1)[1])!=previous_port
            assert blocker.getsockname()[1]==previous_port
        finally:blocker.close()
        check('catalog_crash_and_port_collision_leave_postgres_and_unrelated_listener_usable')
        command('restart');ready()
        for _ in range(3):
            current=owned()['pid'];os.kill(current,signal.SIGKILL)
            wait(lambda:status()['state'] in ('backoff','failed'))
            if status()['state']=='failed':break
            ready()
        assert status()['state']=='failed' and status()['start_attempts']==3
        time.sleep(2);assert status()['start_attempts']==3
        assert cell.sql(branch,'SELECT 42')=='42'
        command('restart');ready()
        check('bounded_restart_budget_and_explicit_recovery')
        with (cellroot/'catalog/process.log').open('ab') as log:log.write(b'x'*(6*1024*1024))
        wait(lambda:(cellroot/'catalog/process.log.1').exists())
        assert (cellroot/'catalog/process.log.1').stat().st_size<=5*1024*1024
        check('bounded_private_process_log_rotation')
        cell.stop();cell.start();restarted=ready()
        assert api(restarted['endpoint'],token(),'catalogs/uc01_recovery')[1]['id']==catalog_id
        assert restarted['metastore_id']==first['metastore_id']
        check('daemon_restart_preserves_catalog_metadata')
        external=Server(runtime,root/'external',{'s3_access':'uc01-test-access','s3_secret':'uc01-test-secret'})
        external.start();external_token=external.root/'etc/conf/token.txt';external_token.chmod(0o600)
        external_id=external.ok('GET','metastore_summary')['metastore_id']
        proxy,thread,ca=tls_proxy(root,external.base)
        provider=dict(mode='external',endpoint=f'https://127.0.0.1:{proxy.server_port}',token_file=str(external_token),ca_file=None,metastore_id=external_id)
        command('configure',provider=provider)
        wait(lambda:status()['state']=='unavailable')
        assert status()['error']=='connection_failed'
        provider['ca_file']=str(ca);provider['metastore_id']='00000000-0000-4000-8000-000000000001'
        command('configure',provider=provider)
        wait(lambda:status()['error']=='metastore_identity_changed')
        provider['metastore_id']=external_id;command('configure',provider=provider);outside=ready()
        assert outside['mode']=='external' and not outside['capabilities']['cross_host_storage']
        assert not any(r['role']=='unity-catalog' for r in cell.records())
        check('external_tls_ca_authentication_identity_and_disabled_storage')
        original=external_token.read_bytes();external_token.write_bytes(b'invalid-credential-replacement')
        wait(lambda:status()['error']=='credential_rejected')
        external_token.write_bytes(original);ready()
        check('external_credential_reference_rotation_without_restart')
        cell.stop();assert external.process.poll() is None
        assert external.ok('GET','metastore_summary')['metastore_id']==external_id
        check('down_never_stops_operator_managed_service')
        cell.start();ready();command('configure',provider=dict(mode='local',runtime=None));ready()
        cell.daemons[-1].kill();cell.daemons[-1].wait(timeout=5)
        subprocess.run([str(cell.binary),'down','--data-dir',str(cellroot)],check=True,capture_output=True,timeout=60)
        assert not cell.records();assert external.process.poll() is None
        check('down_recovers_owned_catalog_after_daemon_crash')
        report['status']='PASS'
    finally:
        if proxy:proxy.shutdown();proxy.server_close();thread.join(timeout=3)
        if external:external.stop()
        cell.close()
        for daemon in cell.daemons:
            if daemon.poll() is None:
                daemon.terminate()
                try:daemon.wait(timeout=10)
                except subprocess.TimeoutExpired:daemon.kill();daemon.wait(timeout=5)
        subprocess.run([str(cell.binary),'down','--data-dir',str(cellroot)],check=True,capture_output=True,timeout=60)
        assert not cell.records(), 'owned services survived fixture cleanup'
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS':shutil.rmtree(root)
    print(json.dumps(dict(status=report['status'],checks=len(report['checks']))))


if __name__=='__main__':main()
