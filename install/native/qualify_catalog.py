#!/usr/bin/env python3
"""UC08: unchanged curl-installed candidate, native catalog, browser and recovery."""
import argparse
from functools import partial
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from qualify import Handler, run
from stage import stage
from diagnostics import summarize
from demo import verify as demo
from unity_catalog import verify as verify_uc

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def qualify(args):
    root = Path(tempfile.mkdtemp(prefix='s8-', dir='/tmp')).resolve()
    root.chmod(0o700)
    web = root/'web'; web.mkdir()
    prefix = root/'programs with spaces'
    key = root/'preview.pem'
    report = dict(status='failed', checks=[], network_evidence=args.network_evidence,
                  limits=['local owner; no multi-user isolation or governed IAM claim',
                          'sampled daemon descendants; RSS can double-count shared pages and miss short peaks'])
    server = sampler = None
    env = {k:v for k,v in os.environ.items() if not k.startswith(('PG','AWS_','PC_','OTEL_','JAVA','JDK','SUPABRICKS_')) and not k.upper().endswith('_PROXY')}
    env.update(SUPABRICKS_INSTALL_DIR=str(prefix), SUPABRICKS_NO_MODIFY_PATH='1',
               SUPABRICKS_DATA_DIR=str(root/'installer-data'), JAVA_HOME=str(root/'absent-java'),
               SB_UC00_NETWORK_EVIDENCE=args.network_evidence,
               SUPABRICKS_CONSOLE_NETWORK_EVIDENCE=args.network_evidence)
    try:
        archive = args.directory/f'supabricks-{args.version}-{args.target}.tar.gz'
        archive_sha = digest(archive)
        assert archive_sha == Path(str(archive)+'.sha256').read_text().split()[0]
        for p in (archive, Path(str(archive)+'.sha256')): shutil.copy2(p, web/p.name)
        report['archive'] = dict(version=args.version, target=args.target, sha256=archive_sha)
        run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',key])
        server = ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(web)))
        base=f'http://127.0.0.1:{server.server_port}'
        stage(web,args.version,base,key)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        curl=subprocess.Popen(['curl','-fsSL',base+'/install.sh'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        bash=subprocess.run(['bash'],env=env,stdin=curl.stdout,capture_output=True,timeout=300)
        curl.stdout.close();curl.wait(timeout=10)
        assert curl.returncode==0 and bash.returncode==0, 'localhost curl installation failed'
        installed=(prefix/'current').resolve()
        binary=prefix/'bin/supabricks'
        identity=json.loads(run([binary,'installation','verify'],env=env))['identity']
        report.update(release_identity=identity, source=json.loads((installed/'release.json').read_text())['provenance'],
                      unity_catalog=verify_uc(installed/'share/unity-catalog',args.target),
                      uc_build_sha256=digest(installed/'share/unity-catalog/build.json'), demo=demo(installed))
        report['checks'].append('localhost_curl_install_path_with_spaces_verified')
        # The exact installed JRE executable is asserted by the native service gate;
        # JAVA_HOME is absent and the OS qualification denies system Java execution.
        stop=root/'sample.stop'; measurement=root/'sample.json'
        sampler=subprocess.Popen([str(installed/'python/analytics/python'),str(ROOT/'install/native/measure.py'),
            '--data',str(root),'--tree-root','--stop',str(stop),'--report',str(measurement)],env=env)
        fixture=root/'f';fixture.mkdir()
        reports={}
        for name in ('service','browser','recovery'):
            report['active_gate']=name
            path=root/(name+'.json')
            if name=='browser':
                command=[args.node,str(args.console/'scripts/qualify-catalog.mjs'),'--binary',str(binary),
                         '--root',str(fixture),'--report',str(path)]
            else:
                command=[str(installed/'python/analytics/python'),str(ROOT/f'e2e/native/catalog/{name}.py'),
                    '--exact-installed','--release',str(installed),'--binary',str(binary),
                    '--uc-runtime',str(installed/'share/unity-catalog'),'--workspace',str(fixture),'--report',str(path)]
                if name=='recovery' and args.disk_full: command.append('--disk-full')
            cleanup=root/(name+'.cleanup.json')
            command=[str(installed/'python/analytics/python'),str(ROOT/'install/native/catalog_gate.py'),
                     '--report',str(cleanup),'--',*command]
            started=time.monotonic()
            with (root/(name+'.private.log')).open('w') as log:
                result=subprocess.run(command,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1550)
            value=json.loads(path.read_text()) if path.exists() else {}
            # Only check identities, typed outcomes and measured fields leave the private fixture.
            checks=[x['name'] if isinstance(x,dict) else x for x in value.get('checks',[])]
            reports[name]=dict(status=value.get('status','FAIL'), checks=checks, exit_code=result.returncode,
                               duration_seconds=time.monotonic()-started, report_sha256=digest(path) if path.exists() else None)
            reports[name]['cleanup']=json.loads(cleanup.read_text()) if cleanup.exists() else None
            report['suites']=reports
            assert result.returncode==0 and value.get('status')=='PASS', f'{name} catalog gate failed'
            if name!='browser': assert value['release_identity']==identity
            if name=='service':
                bootstrap=next(x for x in value['checks'] if x['name']=='installed_private_jre_authenticated_loopback_bootstrap')
                report['bootstrap']={k:bootstrap[k] for k in ('readiness_seconds','idle_rss_bytes')}
                report['contract']=value['contract']
            report['checks'].append('exact_installed_'+name)
        stop.touch();assert sampler.wait(timeout=20)==0;sampler=None
        report['measurements']=json.loads(measurement.read_text())
        assert report['measurements']['catalog_processes_observed']>0
        assert report['measurements']['catalog_peak_rss_bytes']>0
        assert json.loads(run([binary,'installation','verify'],env=env))['identity']==identity
        assert digest(archive)==archive_sha
        report['checks'].append('archive_and_installed_inventory_unchanged')
        report.pop('active_gate',None)
        report['status']='passed'
    except Exception as error:
        report['failure']=dict(type=type(error).__name__,gate=report.get('active_gate','installation'))
        report['diagnostics']={name:summarize(root/(name+'.private.log')) for name in ('service','browser','recovery')}
        raise
    finally:
        if sampler:
            (root/'sample.stop').touch()
            try:sampler.wait(timeout=20)
            except subprocess.TimeoutExpired:sampler.kill();sampler.wait()
        if server:server.shutdown();server.server_close()
        key.unlink(missing_ok=True)
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='passed':shutil.rmtree(root)
        else:print('Private catalog qualification workspace:',root,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('directory','console','report'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--version',default='v0.1.0-alpha.34')
    p.add_argument('--target',choices=('linux-x86_64','macos-arm64'),required=True)
    p.add_argument('--node',required=True)
    p.add_argument('--network-evidence',required=True)
    p.add_argument('--disk-full',action='store_true')
    qualify(p.parse_args())
