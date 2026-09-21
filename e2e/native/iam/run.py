#!/usr/bin/env python3
"""IAM00 Linux feasibility; isolated temporary state, no installed-cell migration."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import sys
import tempfile
import tarfile
import hashlib
import time
import psutil
from qualify import validate

from common import HERE, PINS, Evidence, command, digest
import identity
import postgres
import sandbox

# Reuse the pinned native-cell fixture, including descendant-aware shutdown.
sys.path.insert(0,str(HERE.parent/'catalog'))
from probe import CatalogCell
from server import Server

REPO=HERE.parents[2]


def verify(release,tools):
    assert digest(release/'release.json')==PINS['platform']['inventory_sha256'], 'product inventory differs from pin'
    inventory=json.loads((release/'release.json').read_text())
    for name,entry in inventory['files'].items():
        file=release/name
        assert file.resolve().is_relative_to(release)
        assert digest(file)==entry['sha256'],'release inventory differs: '+name
    archive=tools/'gvisor.tar.bz2'
    assert digest(archive,'sha512')==PINS['gvisor']['sha512'], 'gVisor archive differs from pin'
    with tarfile.open(archive) as tar:
        for entry in tar.getmembers():
            if not entry.isfile(): continue
            file=tools/entry.name
            assert file.resolve().is_relative_to(tools)
            with tar.extractfile(entry) as stream:
                expected=hashlib.file_digest(stream,'sha256').hexdigest()
            assert digest(file)==expected, 'gVisor file changed after preparation'
    assert PINS['gvisor']['version'] in command(tools/'runsc','--version')
    return inventory


def provenance(path):
    value=json.loads(path.read_text())
    keys=('repository','commit','source_commit','source_dirty','source_pin_sha256','source_lock_sha256',
          'dependency_lock_sha256','version','target','builder_script_sha256')
    return dict(build_manifest_sha256=digest(path),**{key:value[key] for key in keys if key in value})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--tools',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args()
    assert sys.platform=='linux' and platform.machine()=='x86_64','Linux x86_64 only'
    os.umask(0o077)
    def interrupted(signum, frame): raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,interrupted)
    release,tools=args.release.resolve(),args.tools.resolve()
    inventory=verify(release,tools)
    root=Path(tempfile.mkdtemp(prefix='sb-iam00-',dir='/tmp'))
    print('Private diagnostics: '+str(root),flush=True)
    evidence=Evidence()
    report=dict(schema_version=1,status='FAIL',profile='iam00-feasibility-only',
        governed_product_enabled=False,pins=PINS,host=dict(kernel=platform.release(),machine=platform.machine(),
            logical_cpus=os.cpu_count(),memory_bytes=psutil.virtual_memory().total),
        source_sha256={str(p.relative_to(REPO)):digest(p) for p in sorted(HERE.glob('*')) if p.is_file()},
        release_inventory_sha256=digest(release/'release.json'),
        platform_revision=inventory['provenance']['platform_commit'],
        sail=provenance(release/'provenance/sail/sail-build.json'),
        catalog=provenance(release/'share/unity-catalog/build.json'),
        python_packages={p:importlib.metadata.version(p) for p in ('psutil','cryptography','psycopg','boto3')},
        phases={},evidence=vars(evidence))
    cellroot=root/'cell'; cellroot.mkdir(mode=0o700)
    cell=CatalogCell(release/'bin/supabricks',release/'engine',release/'helpers',cellroot)
    cell.work=root/'project'; cell.work.mkdir()
    idp=identity.Keycloak(root,'iam00-keycloak-'+root.name)
    server=None
    stage='preflight'; started=time.monotonic()
    try:
        catalog_pin=json.loads((REPO/'components/unity-catalog-source.lock.json').read_text())
        sail_pin=json.loads((REPO/'components/sail-source.lock.json').read_text())
        assert report['catalog']['source_commit']==catalog_pin['commit']
        assert report['sail']['commit']==sail_pin['commit']
        assert not report['sail']['source_dirty'] and not report['catalog']['source_dirty']
        stage='postgres'; cell.start()
        ports=postgres.probe(cell,root,evidence); report['phases'][stage]='PASS'
        stage='identity'; idp.start()
        server=Server(release/'share/unity-catalog',root/'uc',cell.config)
        config=root/'uc/etc/conf/server.properties'
        config.write_text('server.env=dev\nserver.authorization=enable\nserver.allowed-issuers='+idp.issuer+
            '\nserver.audiences=supabricks\nserver.access-token-timeout=PT5M\n')
        server.start()
        identity.probe(idp,server,root,evidence,release)
        report['fixture_config_sha256']=dict(keycloak=digest(root/'realm.json'),uc=digest(config))
        report['phases'][stage]='PASS'
        stage='sandbox'; sandboxroot=root/'sandbox'; sandboxroot.mkdir()
        sandbox.probe(release,tools,sandboxroot,evidence,
            [['127.0.0.1',p] for p in [*ports,server.port,server.port+1,int(idp.base.rsplit(':',1)[1])]])
        report['phases'][stage]='PASS'
        report['status']='PASS_WITH_LIMITATIONS'
    except BaseException as error:
        report['phases'][stage]='FAIL'
        report['failure']=dict(stage=stage,type=type(error).__name__)
        raise
    finally:
        cleanup=[]
        owned=set()
        for child in [*cell.daemons, *([server.process] if server and server.process else [])]:
            if child.poll() is not None: continue
            try:
                parent=psutil.Process(child.pid)
                owned.add(parent); owned.update(parent.children(recursive=True))
            except psutil.NoSuchProcess: pass
        for name,stop in [('catalog',lambda: server.stop() if server else None),('keycloak',idp.stop),
                          ('cell',lambda: cell.stop() if (cellroot/'state.sqlite3').exists() else None)]:
            try: stop(); cleanup.append(dict(name=name,status='PASS'))
            except BaseException as error:
                cleanup.append(dict(name=name,status='FAIL',type=type(error).__name__))
                report['status']='FAIL'
        # A startup failure may prevent native orderly shutdown. Reap only this run's
        # captured PID/birth identities; never signal an installed cell or reused PID.
        leaked=[]
        for process in owned:
            try:
                if process.is_running() and process.status()!=psutil.STATUS_ZOMBIE:
                    leaked.append(process); process.kill()
            except psutil.NoSuchProcess: pass
        _,alive=psutil.wait_procs(leaked,timeout=10)
        if alive:
            cleanup.append(dict(name='owned_descendants',status='FAIL'))
            report['status']='FAIL'
        report['cleanup']=cleanup
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        if report['status']=='PASS_WITH_LIMITATIONS':
            try: validate(report)
            except AssertionError:
                report['status']='FAIL'
                report['failure']=dict(stage='evidence_validation',type='AssertionError')
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
    assert report['status']=='PASS_WITH_LIMITATIONS','capability qualification failed; inspect the report failure stage'
    print('IAM00 capability report: '+str(args.report),flush=True)


if __name__=='__main__': main()
