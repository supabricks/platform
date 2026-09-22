#!/usr/bin/env python3
"""UC09.8: curl-install and exercise one unchanged archive in an offline Linux cell.

The Docker socket belongs to this trusted test controller, never to a workload.
All candidate executables, console assets, Python and UC come from the archive.
"""
import argparse
from functools import partial
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from qualify import Handler, run
from stage import stage
from diagnostics import summarize

ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT/'components/execution-runtime.lock.json').read_text())
IDP = json.loads((ROOT/'e2e/native/iam/pins.lock.json').read_text())['keycloak']['image']
TARGET = 'linux-x86_64'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def docker_json(*args):
    return json.loads(subprocess.check_output(['docker', *map(str, args)], text=True))


def inside(args):
    root = args.workspace
    report = dict(schema_version=1, status='failed', profile='linux-governed-shared-v1',
                  target=TARGET, checks=[], suites={})
    server = None
    os.umask(0o077)
    try:
        controller = docker_json('inspect', os.environ['SUPABRICKS_QUALIFIER_CONTAINER'])[0]
        assert controller['HostConfig']['NetworkMode'] == 'none'
        assert not controller['HostConfig']['Privileged']
        refused = 0
        for address in [('1.1.1.1',443), ('8.8.8.8',53)]:
            try:
                socket.create_connection(address, timeout=2).close()
            except OSError:
                refused += 1
        assert refused == 2, 'qualification has external network access'
        report['network'] = dict(mode='none', external_attempts=2, external_denials=2,
                                 internal_identity='TLS Keycloak in the same isolated namespace')
        report['qualifier_image'] = controller['Image']
        web=root/'web';web.mkdir()
        archive=args.directory/f'supabricks-{args.version}-{TARGET}.tar.gz'
        archive_hash=digest(archive)
        assert Path(str(archive)+'.sha256').read_text()==f'{archive_hash}  {archive.name}\n'
        for path in [archive, Path(str(archive)+'.sha256')]:shutil.copyfile(path,web/path.name)
        key=root/'installer-key.pem'
        run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:3072','-out',key])
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Handler,directory=str(web)))
        base=f'http://127.0.0.1:{server.server_port}'
        stage(web,args.version,base,key)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        prefix=root/'installed programs'
        env=dict(os.environ,SUPABRICKS_INSTALL_DIR=str(prefix),SUPABRICKS_NO_MODIFY_PATH='1',
                 SUPABRICKS_DATA_DIR=str(root/'installer-data'))
        curl=subprocess.Popen(['curl','-fsSL',base+'/install.sh'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        bash=subprocess.run(['bash'],stdin=curl.stdout,capture_output=True,env=env,timeout=300)
        curl.stdout.close();curl.wait(timeout=10)
        assert curl.returncode==0 and bash.returncode==0, 'signed curl installation failed'
        release=(prefix/'current').resolve();binary=release/'bin/supabricks'
        identity=json.loads(run([binary,'installation','verify'],env=env))['identity']
        manifest=json.loads((release/'release.json').read_text())
        # The guest is UID 1000; the installing server owner need not be. Only
        # public product files are readable through this read-only mount. Host
        # ancestors remain private, as do all data and execution configuration.
        for path in [release,*release.rglob('*')]:
            needed=5 if path.is_dir() or (path.stat().st_mode & 0o111) else 4
            assert path.stat().st_mode & needed==needed, 'installed product is not readable by the guest UID'
        report['checks'].append('installed_payload_readable_by_distinct_guest_uid')
        report.update(release_identity=identity,archive=dict(version=args.version,target=TARGET,sha256=archive_hash),
                      source=manifest['provenance'],binary_sha256=digest(binary),
                      console_manifest_sha256=digest(release/'share/console/console.json'))
        report['checks'].append('signed_curl_install_and_verified_unchanged_candidate')
        source_config=json.loads(args.execution_config.read_text())
        runtime=root/'execution-runtime.json'
        run([release/'python/analytics/python', release/'share/governed/prepare-runtime.py',
             '--gvisor-archive',Path(source_config['tools'])/'gvisor.tar.bz2',
             '--output',root/'runtime-inputs','--config',runtime],timeout=180)
        config=json.loads(runtime.read_text())
        assert config['inventory_sha256']==identity
        report['checks'].append('installed_private_runtime_preparation_without_downloads')
        assert digest(Path(config['tools'])/'verified.json')==PINS['gvisor']['inventory_sha256']
        assert digest(config['rootfs'])==config['rootfs_sha256']
        report['components']=dict(keycloak=IDP,rootfs_image=PINS['rootfs'],rootfs_sha256=config['rootfs_sha256'],
            gvisor_inventory_sha256=config['tools_sha256'],execution_config_sha256=digest(runtime),
            execution_release_identity=config['inventory_sha256'],
            execution_pin_sha256=digest(ROOT/'components/execution-runtime.lock.json'),
            installer_template_sha256=digest(ROOT/'install/native/install.sh.in'),
            supervisor_sha256=digest(ROOT/'python/execution/supervisor.py'),
            workload_sha256=digest(ROOT/'python/execution/workload.py'))
        common=['--binary',str(binary)]
        suites={
            'identity':[sys.executable,str(ROOT/'e2e/native/identity/qualify.py'),*common,'--exact-installed','--uc-runtime',str(release/'share/unity-catalog'),'--execution-config',str(runtime)],
            'data':[sys.executable,str(ROOT/'e2e/native/governed/qualify.py'),*common,'--release',str(release),'--exact-installed'],
            'upgrade':[sys.executable,str(ROOT/'install/native/governed_upgrade.py'),'--release',str(release),'--previous-release',source_config['release']],
            'browser':[sys.executable,str(ROOT/'e2e/native/governed-console/qualify.py'),*common,'--release',str(release),'--uc-runtime',str(release/'share/unity-catalog'),'--execution-config',str(runtime),'--console',str(args.console),'--exact-installed','--tls'],
        }
        if args.slice:suites={args.slice:suites[args.slice]}
        for name, command in suites.items():
            print('Qualifying installed governed '+name,flush=True)
            path=root/(name+'.json');cleanup=root/(name+'-cleanup.json')
            command += ['--output' if name=='identity' else '--report',str(path)]
            started=time.monotonic()
            with (root/(name+'.private.log')).open('w') as log:
                result=subprocess.run([sys.executable,str(ROOT/'install/native/catalog_gate.py'),'--timeout','1200',
                    '--data-root',str(root),'--report',str(cleanup),'--',*command],stdout=log,stderr=subprocess.STDOUT,timeout=1250)
            value=json.loads(path.read_text()) if path.exists() else {}
            census=json.loads(cleanup.read_text()) if cleanup.exists() else {}
            suite=dict(status=value.get('status','failed'),checks=value.get('checks',[]),exit_code=result.returncode,
                       cleanup=census,report_sha256=digest(path) if path.exists() else None,
                       release_identity=identity,binary_sha256=value.get('binary_sha256'),duration_seconds=time.monotonic()-started)
            if result.returncode:
                suite['diagnostics']=summarize(root/(name+'.private.log'))
            if name=='identity':suite['measurements']=value.get('measurements',{})
            if name=='upgrade':suite['predecessor_identity']=value.get('predecessor_identity')
            if name=='browser':
                for field in ['revocation_observed_ms','resource_envelope','execution_memory','tls','exact_installed','console_manifest_sha256']:
                    suite[field]=value.get(field)
            report['suites'][name]=suite
            assert result.returncode==0 and value.get('status') in ('PASS','passed'), name+' failed; inspect private diagnostics'
            if name=='browser':assert value.get('release_identity')==identity
            assert value.get('binary_sha256')==report['binary_sha256'], name+' used another binary'
            assert census.get('leaked_descendants')==0 and census.get('remaining_descendants')==0
            report['checks'].append('exact_installed_'+name)
        assert json.loads(run([binary,'installation','verify']))['identity']==identity
        assert digest(archive)==archive_hash
        report['checks'].append('archive_and_installed_inventory_unchanged_after_all_suites')
        report['status']='partial' if args.slice else 'passed'
    except Exception as error:
        report['failure']=dict(type=type(error).__name__)
        raise
    finally:
        if server:server.shutdown();server.server_close()
        args.report.write_text(json.dumps(report,indent=2)+'\n')


def outer(args):
    root=Path(tempfile.mkdtemp(prefix='s9-')).resolve();root.chmod(0o700)
    for name in ['t','home']:(root/name).mkdir()
    print('Private UC09.8 diagnostics: '+str(root),flush=True)
    name='sb-uc098-'+uuid.uuid4().hex[:12]
    config=json.loads(args.execution_config.read_text())
    mounts={ROOT,args.directory.resolve(),args.console.resolve(),args.execution_config.resolve(),
            Path(config['tools']).resolve(),Path(config['rootfs']).resolve(),Path(config['release']).resolve()}
    # Preparation is online; the execution phase below has no external network.
    for image in [IDP,PINS['rootfs']]:
        subprocess.run(['docker','image','inspect',image],check=True,stdout=subprocess.DEVNULL)
    command=['docker','run','--rm','--name',name,'--network','none','--init',
             '--user',f'{os.getuid()}:{os.getgid()}','--group-add',str(Path('/var/run/docker.sock').stat().st_gid),
             '--memory','16g','--memory-swap','16g','--cpus','4','--pids-limit','4096',
             '--mount',f'type=bind,source={root},target={root}',
             '--mount','type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
             '-e','SUPABRICKS_QUALIFIER_CONTAINER='+name,'-e','HOME='+str(root/'home'),'-e','TMPDIR='+str(root/'t')]
    for path in sorted(mounts):command += ['--mount',f'type=bind,source={path},target={path},readonly']
    command += [args.image,'python3',str(Path(__file__).resolve()),'--inside','--workspace',str(root),
                '--directory',str(args.directory.resolve()),'--version',args.version,'--console',str(args.console.resolve()),
                '--execution-config',str(args.execution_config.resolve()),'--report',str(root/'governed.json')]
    if args.slice:command += ['--slice',args.slice]
    try:
        result=subprocess.run(command,timeout=4000)
    finally:
        leaked=[]
        identifiers=subprocess.check_output(['docker','ps','-aq'],text=True).split()
        for identifier in identifiers:
            info=docker_json('inspect',identifier)[0]
            if info['Name']=='/'+name or any(m.get('Source','').startswith(str(root)+'/') for m in info['Mounts']):
                leaked.append(identifier)
                subprocess.run(['docker','rm','-f',identifier],check=True,stdout=subprocess.DEVNULL)
        report=json.loads((root/'governed.json').read_text()) if (root/'governed.json').exists() else dict(status='failed')
        report['cleanup']=dict(leaked_containers=len(leaked),remaining_containers=0)
        # Host capacity includes Docker siblings (IdP and execution sandboxes),
        # which are deliberately outside the trusted controller's cgroup.
        memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')))*1024
        report['host_capacity']=dict(cpu_count=len(os.sched_getaffinity(0)),memory_bytes=memory)
        if leaked:report['status']='failed'
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
    assert result.returncode==0 and report['status'] in ('passed','partial'), 'installed governed qualification failed'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ['directory','console','execution-config','report']:parser.add_argument('--'+field,type=Path,required=True)
    parser.add_argument('--version',default='v0.1.0-alpha.35')
    parser.add_argument('--image',default='supabricks-uc098-qualifier')
    parser.add_argument('--workspace',type=Path)
    parser.add_argument('--inside',action='store_true')
    parser.add_argument('--slice',choices=['identity','data','browser','upgrade'])
    args=parser.parse_args()
    (inside if args.inside else outer)(args)
