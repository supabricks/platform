"""N01-only Jupyter lifecycle hook; not an installed platform service."""
import asyncio
import json
import os
from pathlib import Path
import time
import uuid
import psutil
from jupyter_server.services.kernels.kernelmanager import AsyncMappingKernelManager
from tornado.web import HTTPError

CONFIG = json.loads(Path(os.environ['SB_N01_CONFIG']).read_text())


def record(event, **fields):
    with Path(CONFIG['journal']).open('a') as out:
        out.write(json.dumps(dict(event=event, time=time.time(), **fields)) + '\n')


async def cli(*args):
    proc = await asyncio.create_subprocess_exec(CONFIG['binary'], *args, '--project', CONFIG['project'],
        '--data-dir', CONFIG['data'], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 180)
    if proc.returncode:
        raise RuntimeError('owned analytical action failed: ' + stderr.decode()[-500:])
    return json.loads(stdout.decode().splitlines()[-1])


class OwnedKernels(AsyncMappingKernelManager):
    """Prove pre-launch admission and compensation with real A03 actions."""
    async def start_kernel(self, *, kernel_id=None, path=None, **kwargs):
        if kernel_id is not None:
            raise HTTPError(400, 'Browser cannot select an existing kernel for startup')
        if kwargs.get('kernel_name') != 'supabricks-probe' or len(self) >= 2:
            raise HTTPError(409, 'Only the bounded Supabricks probe kernel is admitted')
        identity = str(uuid.uuid4())
        record('admitting', identity=identity)
        session = await cli('analytics', 'open', '--branch', 'main', '--wait', '--ttl-ms', '600000')
        if session['state'] != 'ready':
            await cli('analytics', 'close', session['id'], '--wait')
            raise HTTPError(503, 'Analytical session did not become ready')
        context = Path(CONFIG['runtime']) / (identity + '.json')
        context.write_text(json.dumps(dict(endpoint=session['endpoint'], epoch_id=session['epoch_id'])))
        context.chmod(0o600)
        record('admitted', identity=identity, session_id=session['id'], epoch_id=session['epoch_id'])
        try:
            fail_next = Path(CONFIG['runtime']) / 'fail-next'
            if fail_next.exists():
                fail_next.unlink()
                raise RuntimeError('N01 injected failure after analytical admission')
            environment = dict(os.environ, SB_N01_CONTEXT=str(context))
            kwargs['env'] = environment
            kwargs['cwd'] = CONFIG['project']
            result = await super().start_kernel(**kwargs)
            kernel = self.get_kernel(result)
            kernel.sb_session = session['id']
            kernel.sb_context = context
            kernel.sb_peak_rss = 0
            async def measure():
                while True:
                    try:
                        process=psutil.Process(kernel.provisioner.pid)
                        kernel.sb_peak_rss=max(kernel.sb_peak_rss,process.memory_info().rss)
                    except psutil.NoSuchProcess: return
                    await asyncio.sleep(.2)
            kernel.sb_measure=asyncio.create_task(measure())
            record('launched', identity=identity, kernel_id=result, session_id=session['id'], pid=kernel.provisioner.pid)
            return result
        except BaseException:
            await cli('analytics', 'close', session['id'], '--wait')
            context.unlink(missing_ok=True)
            record('start_failed', identity=identity)
            raise

    async def shutdown_kernel(self, kernel_id, *args, **kwargs):
        kernel = self.get_kernel(kernel_id)
        session = getattr(kernel, 'sb_session', None)
        context = getattr(kernel, 'sb_context', None)
        await super().shutdown_kernel(kernel_id, *args, **kwargs)
        if hasattr(kernel,'sb_measure'): kernel.sb_measure.cancel()
        if session:
            await cli('analytics', 'close', session, '--wait')
        if context:
            context.unlink(missing_ok=True)
        record('stopped', kernel_id=kernel_id, session_id=session, peak_rss_bytes=getattr(kernel,'sb_peak_rss',None))
