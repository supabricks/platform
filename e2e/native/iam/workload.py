"""Runs INSIDE gVisor: exact packaged managed Jupyter, kernel, bootstrap and Sail.

The probe supplies a one-use launch shim in place of the future host-side adapter.
It is deliberately inside the lease: it has no host control socket or credentials.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

ROOT = Path('/scratch')
PYTHON = '/product/python/runtime/bin/python3.12'
NOTEBOOKS = Path('/product/python/notebooks')


def wait(action, timeout=90):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if action(): return
        time.sleep(.1)
    raise TimeoutError('workload readiness deadline')


def write(path, value):
    path.write_text(json.dumps(value)); path.chmod(0o600)


def main():
    began = time.monotonic()
    for name in ('commands','reports','kernels','runtime','ipython','empty-contents','data'):
        (ROOT/name).mkdir(exist_ok=True)
    subprocess.run([PYTHON, '-m', 'venv', '--without-pip', '--system-site-packages', str(ROOT/'env')], check=True)
    python = str(ROOT/'env/bin/python')
    # Both tenants deliberately use the same Connect port in separate network namespaces.
    sail = subprocess.Popen([PYTHON, '/probe/sandbox_sail.py'], stdout=(ROOT/'sail.log').open('w'), stderr=subprocess.STDOUT)
    def sail_ready():
        if sail.poll() is not None: raise RuntimeError('Sail startup failed')
        return (ROOT/'sail-ready').exists()
    wait(sail_ready)
    kernel = str(uuid.uuid4())
    ready = ROOT/'kernels'/f'{kernel}.ready.json'
    context = ROOT/'kernels'/f'{kernel}.context.json'
    fault = ROOT/'kernels'/f'{kernel}.fault.json'
    write(context, dict(endpoint='sc://127.0.0.1:15002/;session_id=' + (ROOT/'sail-ready').read_text(), epoch_id='iam00', environment='iam00-pinned',
                        python_prefix=str(ROOT/'env'), ready=str(ready), fault=str(fault)))
    gate_token = secrets.token_urlsafe(32)
    launch = ROOT/'kernels'/f'{kernel}.launch.json'
    # No product daemon socket is present. Only this fixed admitted kernel is launchable.
    write(launch, dict(argv=[python, '-I', '-B', str(NOTEBOOKS/'kernel.py'), '-f',
        str(ROOT/'runtime'/f'kernel-{kernel}.json'),
        '--IPKernelApp.exec_files=[' + json.dumps(str(NOTEBOOKS/'bootstrap.py')) + ']'], token=gate_token,
        env=dict(PATH='/usr/bin:/bin', HOME='/scratch', IPYTHONDIR='/scratch/ipython',
                 SUPABRICKS_NOTEBOOK_CONTEXT=str(context), OTEL_SDK_DISABLED='true')))
    shim = ROOT/'launch'
    shim.write_text('#!/product/python/runtime/bin/python3.12\n'
        'import json,os,sys\nfrom pathlib import Path\n'
        'p=Path(sys.argv[3]); v=json.loads(p.read_text()); p.unlink()\n'
        'assert os.environ["SUPABRICKS_PROCESS_TOKEN"]==v["token"]\n'
        'os.execve(v["argv"][0],v["argv"],v["env"])\n')
    shim.chmod(0o700)
    write(ROOT/'kernels'/f'{kernel}.gate.json', dict(binary=str(shim), launch=str(launch),
        token=gate_token, ready=str(ready), environment='iam00-pinned', epoch_id='iam00'))
    settings = ROOT/'settings.json'
    write(settings, dict(workspace=str(ROOT), frame_bytes=2*1024*1024, cell_output_bytes=1024*1024,
                        port=15001, token=secrets.token_urlsafe(32)))
    env = dict(os.environ, HOME='/scratch', JUPYTER_CONFIG_DIR='/scratch/config',
        JUPYTER_DATA_DIR='/scratch/data', JUPYTER_RUNTIME_DIR='/scratch/runtime', IPYTHONDIR='/scratch/ipython')
    server = subprocess.Popen([PYTHON, '-I', '-B', str(NOTEBOOKS/'server.py'), str(settings)], env=env,
        stdout=(ROOT/'jupyter.log').open('w'), stderr=subprocess.STDOUT)
    wait(lambda: (ROOT/'ready.json').exists())
    write(ROOT/'commands'/'start.json', dict(action='start',kernel_id=kernel,generation=1))
    report_path = ROOT/'reports'/f'{kernel}.json'
    def kernel_ready():
        if server.poll() is not None or sail.poll() is not None:
            raise RuntimeError('managed process exited')
        if not report_path.exists(): return False
        value = json.loads(report_path.read_text())
        if value.get('error'): raise RuntimeError('managed bootstrap rejected: '+value['error'])
        return value.get('ready')
    wait(kernel_ready)
    startup = time.monotonic() - began
    from jupyter_client import BlockingKernelClient
    client = BlockingKernelClient(connection_file=str(ROOT/'runtime'/f'kernel-{kernel}.json'))
    client.load_connection_file(); client.start_channels(); client.wait_for_ready(timeout=20)
    # Execute through a real kernel; output stays in the private lease directory.
    code = Path('/probe/kernel_checks.py').read_text()
    message = client.execute(code, allow_stdin=False, stop_on_error=True)
    end = time.monotonic() + 120
    while time.monotonic() < end:
        response = client.get_shell_msg(timeout=120)
        if response['parent_header'].get('msg_id') == message:
            if response['content']['status'] != 'ok':
                write(ROOT/'kernel-error.json',response['content'])
                raise RuntimeError('kernel checks failed')
            break
    else: raise TimeoutError('kernel checks response')
    result = json.loads((ROOT/'kernel-results.json').read_text())
    import psutil
    result['notebook_startup_seconds'] = round(startup,3)
    result['post_read_process_rss_bytes'] = sum(p.memory_info().rss for p in psutil.Process().children(recursive=True))
    result['kernel_pid'] = json.loads((ROOT/'kernel-pid.json').read_text())['pid']
    result['uid'] = os.getuid()
    # Keep the kernel busy and a file descriptor open until the OUTSIDE watchdog revokes it.
    client.execute("exec(open('/probe/open_file_query.py').read())", allow_stdin=False)
    wait(lambda: (ROOT/'query-started').exists())
    sail_process=psutil.Process(sail.pid)
    cpu=sail_process.cpu_times()
    cpu_before=cpu.user+cpu.system
    def query_running():
        value=json.loads(report_path.read_text())
        current=sail_process.cpu_times()
        return value['state']=='busy' and current.user+current.system-cpu_before >= .15
    wait(query_running,15)
    cpu=sail_process.cpu_times()
    result['running_query_cpu_seconds']=round(cpu.user+cpu.system-cpu_before,3)
    result['kernel_busy_before_revoke']=True
    write(ROOT/'result.json',result)
    (ROOT/'lease-ready').write_text('ready')
    while True:
        if server.poll() is not None or sail.poll() is not None:
            raise RuntimeError('managed process exited while leased')
        time.sleep(.1)


if __name__ == '__main__': main()
