#!/usr/bin/env python3
"""SY08 signed curl install and sync gates against one unchanged native archive."""
import argparse
from functools import partial
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from qualify import Handler,run
from stage import stage
from diagnostics import summarize
from sync_evidence import WORKERS

ROOT=Path(__file__).resolve().parents[2]
SUITES=('triggered','continuous','maintenance','governed')


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def qualify(args):
    root=Path(tempfile.mkdtemp(prefix='sy08-install-',dir='/tmp')).resolve();root.chmod(0o700)
    web=root/'web';web.mkdir();prefix=root/'programs with spaces';key=root/'preview.pem';server=None
    report=dict(status='failed',suites={},network_evidence=args.network_evidence,checks=[])
    env={k:v for k,v in os.environ.items() if not k.startswith(('PG','AWS_','PC_','OTEL_','JAVA','JDK','SUPABRICKS_')) and not k.upper().endswith('_PROXY')}
    env.update(SUPABRICKS_INSTALL_DIR=str(prefix),SUPABRICKS_NO_MODIFY_PATH='1',SUPABRICKS_DATA_DIR=str(root/'installer-data'))
    try:
        archive=args.directory/f'supabricks-{args.version}-{args.target}.tar.gz'
        checksum=sha(archive);assert checksum==Path(str(archive)+'.sha256').read_text().split()[0]
        report['archive']=dict(version=args.version,target=args.target,sha256=checksum)
        for path in (archive,Path(str(archive)+'.sha256')):shutil.copy2(path,web/path.name)
        run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',key])
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(web)))
        base=f'http://127.0.0.1:{server.server_port}';stage(web,args.version,base,key)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        curl=subprocess.Popen(['curl','-fsSL',base+'/install.sh'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        bash=subprocess.run(['bash'],env=env,stdin=curl.stdout,capture_output=True,timeout=300)
        curl.stdout.close();curl.wait(timeout=10)
        assert curl.returncode==0 and bash.returncode==0,'signed sync installation failed'
        release=(prefix/'current').resolve();binary=release/'bin/supabricks'
        identity=json.loads(run([binary,'installation','verify'],env=env))['identity']
        manifest=json.loads((release/'release.json').read_text())
        report.update(release_identity=identity,binary_sha256=sha(binary),source=manifest['provenance'])
        report['worker_inventory']={name:sha(release/'python/analytics'/name) for name in WORKERS}
        for name in WORKERS:assert manifest['files']['python/analytics/'+name]['sha256']==report['worker_inventory'][name]
        report['checks'].append('signed_curl_install_and_bundled_sync_workers_verified')
        for suite in SUITES:
            print('Qualifying installed sync '+suite,flush=True)
            output=root/(suite+'.json');cleanup=root/(suite+'-cleanup.json');log=root/(suite+'.private.log')
            started=time.monotonic()
            with log.open('w') as stream:
                result=subprocess.run([sys.executable,str(ROOT/'install/native/catalog_gate.py'),'--timeout','1200',
                    '--report',str(cleanup),'--',sys.executable,str(ROOT/'e2e/native/installed_sync.py'),
                    '--release',str(release),'--suite',suite,'--report',str(output)],
                    env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=1250)
            value=json.loads(output.read_text()) if output.exists() else {}
            census=json.loads(cleanup.read_text()) if cleanup.exists() else {}
            record=dict(status=value.get('status','FAIL'),checks=value.get('checks',[]),metrics=value.get('metrics',{}),
                host=value.get('host',{}),
                exact_installed=value.get('exact_installed'),release_identity=value.get('release_identity'),
                binary_sha256=value.get('binary_sha256'),exit_code=result.returncode,cleanup=census,
                report_sha256=sha(output) if output.exists() else None,duration_seconds=time.monotonic()-started)
            report['suites'][suite]=record
            if result.returncode:record['diagnostics']=summarize(log)
            assert result.returncode==0 and value.get('status')=='PASS',suite+' sync gate failed'
            assert value.get('exact_installed') is True and value['release_identity']==identity
            assert value['binary_sha256']==report['binary_sha256']
            assert census.get('leaked_descendants')==0 and census.get('remaining_descendants')==0
            report['checks'].append('exact_installed_'+suite)
        assert json.loads(run([binary,'installation','verify'],env=env))['identity']==identity
        assert sha(archive)==checksum
        report['checks'].append('archive_and_installed_inventory_unchanged')
        report['status']='passed'
    finally:
        if server:server.shutdown();server.server_close()
        key.unlink(missing_ok=True)
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='passed':shutil.rmtree(root)
        else:print('Private installed sync workspace:',root,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('directory','report'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--version',default='v0.1.0-alpha.36')
    parser.add_argument('--target',choices=('linux-x86_64','macos-arm64'),required=True)
    parser.add_argument('--network-evidence',required=True)
    qualify(parser.parse_args())
