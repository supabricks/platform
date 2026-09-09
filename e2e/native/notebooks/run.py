#!/usr/bin/env python3
"""N01 real installed runtime + private Jupyter + browser qualification."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import psutil


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


def wait(test, seconds=120):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        try:
            value=test()
            if value:return value
        except (OSError, urllib.error.URLError):pass
        time.sleep(.1)
    raise TimeoutError('N01 readiness timed out')


def main(args):
    # macOS TMPDIR exceeds PostgreSQL's Unix socket path limit.
    root=Path(tempfile.mkdtemp(prefix='sb-n01-',dir='/tmp')).resolve()
    root.chmod(0o700)
    project=root/'project';project.mkdir()
    notebooks=project/'notebooks';notebooks.mkdir()
    runtime=root/'runtime';runtime.mkdir()
    configdir=root/'config';configdir.mkdir()
    kernels=root/'kernels';(kernels/'supabricks-probe').mkdir(parents=True)
    probe=args.probe.resolve();worker=probe/'python/notebooks';python=probe/'python/runtime/bin/python3.12'
    baseline=args.release.resolve();binary=baseline/'bin/supabricks'
    env={k:v for k,v in os.environ.items() if not k.startswith(('PYTHON','JUPYTER','IPYTHON','PG','AWS_','PC_','OTEL_')) and not k.upper().endswith('_PROXY')}
    env.update(PYTHONDONTWRITEBYTECODE='1', JUPYTER_CONFIG_DIR=str(configdir), JUPYTER_DATA_DIR=str(root/'jupyter-data'), JUPYTER_RUNTIME_DIR=str(runtime), JUPYTER_PATH=str(root/'jupyter-data'), IPYTHONDIR=str(root/'ipython'), JUPYTER_NO_CONFIG='1')
    report=dict(status='running',checks=[],network_evidence=os.environ.get('SB_N01_NETWORK_EVIDENCE','host network not isolated; browser loopback request gate'),baseline_release_sha256=hashlib.sha256((baseline/'release.json').read_bytes()).hexdigest())
    def cli(*parts):
        p=subprocess.run([str(binary),*parts,'--project',str(project),'--data-dir',str(root/'data')],env=env,capture_output=True,text=True,timeout=180)
        if p.returncode:raise RuntimeError('N01 CLI '+parts[0]+' failed: '+p.stderr[-500:])
        return json.loads(p.stdout.splitlines()[-1])
    children=[]
    try:
        manifest=json.loads((probe/'probe.json').read_text())
        for name,expected in manifest['files'].items():
            assert hashlib.sha256((probe/name).read_bytes()).hexdigest()==expected,name
        report['checks'].append('relocated_probe_inventory_verified')
        report['probe']=dict(target=manifest['target'],frontend_bytes=manifest['frontend_bytes'],packages=manifest['packages'])
        cli('init','n01-probe');cli('up');cli('database','create','main','--wait')
        cli('sql','--branch','main','--write','--sql','CREATE TABLE public.orders (id int, amount numeric(18,2))')
        cli('sql','--branch','main','--write','--sql','INSERT INTO public.orders VALUES (1, 12.50), (2, 7.25)')
        code="n01_counter = globals().get('n01_counter', 0) + 1\nprint('N01_SPARK_READY')\nassert spark.table('public.orders').count() == 2\nassert str(spark.sql('SELECT sum(amount) AS total FROM public.orders').first().total) == '19.75'\nprint(epoch['epoch_id'])\nspark.table('public.orders').toPandas()"
        image="from IPython.display import display, Image\nimport base64\ndisplay(Image(data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')))"
        import nbformat
        hostile=nbformat.v4.new_code_cell("print('Safe output')",outputs=[nbformat.v4.new_output('display_data',data={'text/html':"<button onclick=\"window.n01Pwned=true\">Untrusted output</button><script>window.n01Pwned=true</script>",'application/javascript':'window.n01Pwned=true'})])
        notebook=nbformat.v4.new_notebook(cells=[nbformat.v4.new_markdown_cell('# Orders on a pinned snapshot'),nbformat.v4.new_code_cell(code),nbformat.v4.new_code_cell(image),hostile],metadata={'kernelspec':{'name':'supabricks-probe','display_name':'Supabricks probe','language':'python'}})
        nbformat.write(notebook,notebooks/'orders.ipynb')
        config=dict(binary=str(binary),python=str(python),project=str(project),notebooks=str(notebooks),data=str(root/'data'),runtime=str(runtime),config_dir=str(configdir),kernels=str(kernels),journal=str(root/'ownership.jsonl'),trace=str(root/'routes.json'),jupyter_port=port(),bridge_port=port(),jupyter_token=secrets.token_hex(32),launch=secrets.token_hex(32),assets=str(probe/'assets'))
        config['origin']='http://127.0.0.1:'+str(config['bridge_port']);config['upstream']='http://127.0.0.1:'+str(config['jupyter_port'])
        conf=root/'probe-config.json';conf.write_text(json.dumps(config));conf.chmod(0o600);env['SB_N01_CONFIG']=str(conf)
        spec={'argv':[str(python),'-E','-s','-B','-m','ipykernel_launcher','-f','{connection_file}',"--IPKernelApp.exec_files=["+repr(str(worker/'bootstrap.py'))+"]"],'display_name':'Supabricks probe','language':'python'}
        (kernels/'supabricks-probe/kernel.json').write_text(json.dumps(spec))
        started=time.monotonic()
        for name in ['server.py','bridge.py']:
            log=(root/(name+'.log')).open('w')
            child=subprocess.Popen([str(python),'-E','-s','-B',str(worker/name),str(conf)],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            children.append(child);log.close()
        request=urllib.request.Request(config['upstream']+'/jupyter/api/kernelspecs',headers={'Authorization':'token '+config['jupyter_token']})
        wait(lambda:urllib.request.urlopen(request,timeout=2).status==200)
        wait(lambda:urllib.request.urlopen(config['origin'],timeout=2).status==200)
        report['server_start_seconds']=time.monotonic()-started
        report['server_idle_rss_bytes']=psutil.Process(children[0].pid).memory_info().rss
        launch=root/'launch.json';launch.write_text(json.dumps(dict(url=config['origin']+'/#launch='+config['launch'],project=str(project),config=str(conf))));launch.chmod(0o600)
        browser_report=args.report.with_suffix('.browser.json')
        subprocess.run([args.node,str(args.harness),'--launch',str(launch),'--report',str(browser_report)],check=True,env=env,timeout=300)
        browser=json.loads(browser_report.read_text());report['browser']=browser
        saved=nbformat.read(notebooks/'orders.ipynb',as_version=4);nbformat.validate(saved)
        assert any(c.get('outputs') for c in saved.cells)
        assert config['jupyter_token'] not in (notebooks/'orders.ipynb').read_text()
        events=[json.loads(line) for line in (root/'ownership.jsonl').read_text().splitlines()]
        report['ownership']=events;report['routes']=json.loads((root/'routes.json').read_text())
        launched=[e for e in events if e['event']=='launched'];stopped=[e for e in events if e['event']=='stopped']
        assert launched and len(launched)==len(stopped)
        for event in events:
            if event['event']=='admitted':
                state=cli('analytics','session',event['session_id']);assert state['state']=='closed',state['state']
        for event in launched:
            assert any(e['event']=='admitted' and e['session_id']==event['session_id'] and e['time']<=event['time'] for e in events)
            assert not psutil.pid_exists(event['pid'])
            state=cli('analytics','session',event['session_id']);assert state['state']=='closed',state['state']
        report['checks']+=['real_A03_admission_precedes_kernel_launch','kernel_shutdown_closes_Sail_and_releases_process','standard_notebook_roundtrip_without_credentials']
        report['status']='passed'
    except BaseException as error:
        report['status']='failed'
        report['failure_type']=type(error).__name__
        raise
    finally:
        for child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGTERM)
                try:child.wait(timeout=15)
                except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
        try:cli('down')
        finally:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(dict(status=report['status'],report=str(args.report),private_workspace=str(root))))
        if report['status']=='passed':shutil.rmtree(root)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--probe',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--node',required=True)
    parser.add_argument('--harness',type=Path,required=True)
    main(parser.parse_args())
