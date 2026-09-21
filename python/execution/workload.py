"""Untrusted per-execution driver: real Jupyter/kernel plus a private Sail server.

The source may execute arbitrary native Python. Nothing in this process is an
admission/authorization boundary; the outer sandbox enforces that boundary.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path('/scratch')
PYTHON = '/product/python/runtime/bin/python3.12'
NOTEBOOKS = Path('/product/python/notebooks')


def write(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def wait(action):
    end = time.monotonic() + 90
    while time.monotonic() < end:
        if action():
            return
        time.sleep(.1)
    raise TimeoutError('sandbox startup deadline')


def sail():
    os.environ.update(SAIL_MODE='local', SAIL_RUNTIME__MEMORY_POOL__TYPE='fair',
        SAIL_RUNTIME__MEMORY_POOL__FAIR__MAX_SIZE='268435456',
        SAIL_RUNTIME__TEMPORARY_FILES__MAX_SIZE='268435456',
        SAIL_RUNTIME__TEMPORARY_FILES__PATHS='["/scratch/spill"]',
        SAIL_CATALOG__LIST='[{type="memory",name="spark_catalog",initial_database=["default"]}]',
        SAIL_CATALOG__DEFAULT_CATALOG='spark_catalog')
    (ROOT/'spill').mkdir()
    from pysail.spark import SparkConnectServer
    from pyspark.sql import SparkSession
    server = SparkConnectServer(ip='127.0.0.1', port=15002)
    server.start()
    spark = SparkSession.builder.remote('sc://127.0.0.1:15002').getOrCreate()
    spark.client.set_retry_policies([])
    spark.sql('CREATE DATABASE _supabricks').collect()
    spark.sql("CREATE VIEW _supabricks.epoch AS SELECT 'isolated' AS epoch_id").collect()
    (ROOT/'sail-ready').write_text(spark.session_id)
    while True:
        time.sleep(1)


def main():
    for name in ('commands','reports','kernels','runtime','ipython','empty-contents','data'):
        (ROOT/name).mkdir()
    subprocess.run([PYTHON, '-m', 'venv', '--without-pip', '--system-site-packages', str(ROOT/'env')], check=True)
    python = str(ROOT/'env/bin/python')
    sail_process = subprocess.Popen([PYTHON, '-I', '-B', __file__, 'sail'], stdout=(ROOT/'sail.log').open('w'), stderr=subprocess.STDOUT)
    def sail_ready():
        if sail_process.poll() is not None:
            raise RuntimeError('Sail unavailable')
        return (ROOT/'sail-ready').exists()
    wait(sail_ready)
    kernel = str(uuid.uuid4())
    ready = ROOT/'kernels'/f'{kernel}.ready.json'
    context = ROOT/'kernels'/f'{kernel}.context.json'
    fault = ROOT/'kernels'/f'{kernel}.fault.json'
    write(context, dict(endpoint='sc://127.0.0.1:15002/;session_id='+(ROOT/'sail-ready').read_text(),
        epoch_id='isolated', environment='isolated', python_prefix=str(ROOT/'env'), ready=str(ready), fault=str(fault)))
    token = uuid.uuid4().hex
    launch = ROOT/'kernels'/f'{kernel}.launch.json'
    write(launch, dict(argv=[python, '-I', '-B', str(NOTEBOOKS/'kernel.py'), '-f', str(ROOT/'runtime'/f'kernel-{kernel}.json'),
        '--IPKernelApp.exec_files=['+json.dumps(str(NOTEBOOKS/'bootstrap.py'))+']'], token=token,
        env=dict(PATH='/usr/bin:/bin', HOME='/scratch', IPYTHONDIR='/scratch/ipython',
                 SUPABRICKS_NOTEBOOK_CONTEXT=str(context), OTEL_SDK_DISABLED='true')))
    # One-use inside-sandbox process gate. It has no connection to host supervision.
    shim = ROOT/'launch'
    shim.write_text('#!'+PYTHON+'\nimport json,os,sys\nfrom pathlib import Path\np=Path(sys.argv[3]);v=json.loads(p.read_text());p.unlink()\nassert os.environ["SUPABRICKS_PROCESS_TOKEN"]==v["token"]\nos.execve(v["argv"][0],v["argv"],v["env"])\n')
    shim.chmod(0o700)
    write(ROOT/'kernels'/f'{kernel}.gate.json', dict(binary=str(shim), launch=str(launch), token=token,
        ready=str(ready), environment='isolated', epoch_id='isolated'))
    settings = ROOT/'settings.json'
    write(settings, dict(workspace=str(ROOT), frame_bytes=262144, cell_output_bytes=131072, port=15001, token=uuid.uuid4().hex))
    env = dict(os.environ, HOME='/scratch', JUPYTER_CONFIG_DIR='/scratch/config', JUPYTER_DATA_DIR='/scratch/data',
        JUPYTER_RUNTIME_DIR='/scratch/runtime', IPYTHONDIR='/scratch/ipython')
    server = subprocess.Popen([PYTHON, '-I', '-B', str(NOTEBOOKS/'server.py'), str(settings)], env=env,
        stdout=(ROOT/'jupyter.log').open('w'), stderr=subprocess.STDOUT)
    wait(lambda: (ROOT/'ready.json').exists())
    write(ROOT/'commands'/'start.json', dict(action='start', kernel_id=kernel, generation=1))
    report = ROOT/'reports'/f'{kernel}.json'
    def kernel_ready():
        if server.poll() is not None:
            raise RuntimeError('Jupyter unavailable')
        if not report.exists():
            return False
        value = json.loads(report.read_text())
        if value.get('error'):
            raise RuntimeError('kernel unavailable')
        return value.get('ready')
    wait(kernel_ready)
    from jupyter_client import BlockingKernelClient
    client = BlockingKernelClient(connection_file=str(ROOT/'runtime'/f'kernel-{kernel}.json'))
    client.load_connection_file()
    client.start_channels()
    client.wait_for_ready(timeout=20)
    source = json.loads(Path('/admission/source.json').read_text())
    if source['kind'] == 'sql':
        cells = ['print(spark.sql('+repr(source['contents'])+').limit(200).toPandas().to_json(orient="records"))']
    else:
        notebook = json.loads(source['contents'])
        cells = [''.join(c['source']) for c in notebook['cells'] if c['cell_type'] == 'code']
    for code in cells:
        request = client.execute(code, allow_stdin=False, stop_on_error=True)
        while True:
            msg = client.get_iopub_msg(timeout=120)
            if msg.get('parent_header', {}).get('msg_id') != request:
                continue
            kind, content = msg['msg_type'], msg['content']
            if kind in ('stream', 'execute_result', 'display_data', 'error'):
                print(json.dumps(dict(type=kind, content=content)), flush=True)
            if kind == 'status' and content['execution_state'] == 'idle':
                break
        reply = client.get_shell_msg(timeout=10)
        if reply['content']['status'] != 'ok':
            raise RuntimeError('source execution failed')
    client.stop_channels()
    server.terminate()
    sail_process.terminate()


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'sail':
        sail()
    else:
        main()
