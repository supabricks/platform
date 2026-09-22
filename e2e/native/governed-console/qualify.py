#!/usr/bin/env python3
"""Real browser UC09.7 qualification: pinned TLS Keycloak, PG17, UC and gVisor."""
import argparse, hashlib, json, os, secrets, shutil, socket, ssl, subprocess, sys, tempfile, time, urllib.request, uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'e2e/native/catalog'))
from probe import CatalogCell
from cell import wait

def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def main():
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ('binary','release','uc-runtime','execution-config','console','report'):p.add_argument('--'+arg,type=Path,required=True)
    args=p.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='sb-uc097-'));print('Private diagnostics: '+str(root),flush=True)
    cellroot=root/'cell';cellroot.mkdir();cell=CatalogCell(args.binary.resolve(),args.release.resolve()/'engine',args.release.resolve()/'helpers',cellroot)
    name='sb-uc097-'+uuid.uuid4().hex[:12];browser=None;started=False
    base=f'https://127.0.0.1:{port()}';issuer=base+'/realms/uc097';origin=f'http://127.0.0.1:{port()}';callback=origin+'/auth/v1/callback'
    password,secret,admin_password=(secrets.token_urlsafe(32) for _ in range(3))
    audience=dict(name='platform-audience',protocol='openid-connect',protocolMapper='oidc-audience-mapper',config={'included.client.audience':'platform','access.token.claim':'true'})
    realm=dict(realm='uc097',enabled=True,sslRequired='all',clients=[dict(clientId='platform',enabled=True,publicClient=False,secret=secret,standardFlowEnabled=True,directAccessGrantsEnabled=False,redirectUris=[callback],protocolMappers=[audience])],users=[dict(username=user,enabled=True,email=user+'@example.test',emailVerified=True,firstName=user,lastName='Fixture',credentials=[dict(type='password',value=password,temporary=False)]) for user in ['alice','bob']])
    (root/'realm.json').write_text(json.dumps(realm));(root/'realm.json').chmod(0o644)
    for f in ('cert.pem','key.pem'):shutil.copyfile(ROOT/'crates/local/tests/fixtures/identity'/f,root/f);(root/f).chmod(0o644)
    (root/'keycloak.env').write_text('KC_BOOTSTRAP_ADMIN_USERNAME=fixture-admin\nKC_BOOTSTRAP_ADMIN_PASSWORD='+admin_password+'\n')
    context=ssl.create_default_context(cafile=str(root/'cert.pem'))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
    def http(url,body=None,token=None):
        headers={}
        if body is not None:body=urllib.parse.urlencode(body).encode();headers['Content-Type']='application/x-www-form-urlencoded'
        if token:headers['Authorization']='Bearer '+token
        with opener.open(urllib.request.Request(url,body,headers),timeout=10) as r:return json.load(r)
    try:
        pin=json.loads((ROOT/'e2e/native/iam/pins.lock.json').read_text())['keycloak']['image']
        subprocess.run(['docker','run','-d','--name',name,'--memory=1g','--cpus=2','--pids-limit=256','-p','127.0.0.1:'+base.rsplit(':',1)[1]+':8443','--env-file',str(root/'keycloak.env'),'-v',str(root/'realm.json')+':/opt/keycloak/data/import/realm.json:ro','-v',str(root/'cert.pem')+':/tls/cert.pem:ro','-v',str(root/'key.pem')+':/tls/key.pem:ro',pin,'start-dev','--import-realm','--http-enabled=false','--https-certificate-file=/tls/cert.pem','--https-certificate-key-file=/tls/key.pem','--hostname='+base],check=True,capture_output=True)
        started=True
        wait(lambda:http(issuer+'/.well-known/openid-configuration'),timeout=110)
        token=http(base+'/realms/master/protocol/openid-connect/token',dict(grant_type='password',client_id='admin-cli',username='fixture-admin',password=admin_password))['access_token']
        alice_subject=http(base+'/admin/realms/uc097/users?username=alice',token=token)[0]['id']
        shutil.copyfile(args.execution_config,cellroot/'execution-runtime.json')
        # Source native fixture uses the pinned analytics helper supplied by release.
        cell.env['SUPABRICKS_ANALYTICS_PYTHON']=str(args.release.resolve()/'python/analytics/python')
        cell.env['SUPABRICKS_ANALYTICS_WORKER']=str(args.release.resolve()/'python/analytics/export.py')
        (cellroot/"analytics.json").write_text(json.dumps(dict(python=str(args.release.resolve()/"python/analytics/python"),worker=str(args.release.resolve()/"python/analytics/export.py"))))
        cell.start()
        cell.request(method='identity_admin',command=dict(action='configure',provider='keycloak',config=dict(issuer=issuer,client_id='platform',client_secret=secret,introspection_url=issuer+'/protocol/openid-connect/token/introspect',redirects=[callback],ca_pem=(root/'cert.pem').read_text())))
        cell.request(method='identity_admin',command=dict(action='bootstrap',issuer=issuer,subject=alice_subject,label='alice'))
        cell.request(method='catalog_service',command=dict(action='configure',provider=dict(mode='local',runtime=str(args.uc_runtime.resolve()))))
        wait(lambda:cell.request(method='catalog_service',command=dict(action='status'))['state']=='ready',timeout=90)
        log=(root/'browser.log').open('w');browser=subprocess.Popen([str(cell.binary),'console','--governed','--provider','keycloak','--redirect',callback,'--data-dir',str(cellroot)],stdout=log,stderr=log)
        wait(lambda:urllib.request.urlopen(origin+'/auth/v1/console',timeout=2).status==200,timeout=15)
        config=dict(origin=origin,password=password,root=str(root),report=str(args.report.resolve()))
        (root/'browser.json').write_text(json.dumps(config))
        result=subprocess.run(['node',str(args.console.resolve()/'scripts/governed-qualify.mjs'),'--config',str(root/'browser.json')],cwd=args.console.resolve(),timeout=600)
        assert result.returncode==0,'browser qualification failed; inspect private diagnostics'
    finally:
        cleanup=[]
        try:
            if browser:browser.terminate();browser.wait(timeout=10)
        except Exception:cleanup.append('browser cleanup failed')
        try:
            if (cellroot/'control.sock').exists():cell.stop()
        except Exception:cleanup.append('native cell cleanup failed')
        try:
            if started:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,check=True)
        except Exception:cleanup.append('identity fixture cleanup failed')
        if cleanup:
            evidence=json.loads(args.report.read_text()) if args.report.exists() else {}
            evidence.update(status='FAIL',cleanup=cleanup)
            args.report.write_text(json.dumps(evidence,indent=2)+'\n')
            raise RuntimeError('; '.join(cleanup))
    evidence=json.loads(args.report.read_text())
    evidence.update(binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        console_manifest_sha256=hashlib.sha256((args.console/'dist/console.json').read_bytes()).hexdigest(),
        execution_config_sha256=hashlib.sha256(args.execution_config.read_bytes()).hexdigest(),
        keycloak=pin,cleanup='PASS')
    args.report.write_text(json.dumps(evidence,indent=2)+'\n')
if __name__=='__main__':main()
