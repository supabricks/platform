"""Private Jupyter service. Daemon-owned launch files are the only kernel admission.

The browser uses standard Jupyter channels through the Rust console bridge.
No runtime package installation, ambient extension discovery or contents access.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid

import zmq
from jupyter_server.services.kernels.kernelmanager import ServerKernelManager
from jupyter_server.serverapp import ServerApp
from jupyter_server.services.kernels.connection.channels import ZMQChannelsWebsocketConnection
from jupyter_server.services.kernels.connection.base import deserialize_msg_from_ws_v1, serialize_msg_to_ws_v1
from traitlets.config import Config
from tornado.ioloop import PeriodicCallback

SETTINGS = json.loads(Path(sys.argv[1]).read_text())
ROOT = Path(SETTINGS['workspace'])
RECORDS = {}
TASKS = set()
FRAME = SETTINGS['frame_bytes']


def now():
    return int(time.time() * 1000)


def atomic(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, allow_nan=False))
    temporary.replace(path)


def publish(kernel):
    record = RECORDS[kernel]
    atomic(ROOT / 'reports' / (kernel + '.json'), {key: record.get(key) for key in
           ['generation', 'ready', 'state', 'activity_ms', 'interrupt_ack_ms', 'error']})


def fail(kernel, reason):
    if kernel in RECORDS:
        RECORDS[kernel]['error'] = reason
        publish(kernel)


def observe(kernel, message, size):
    record = RECORDS.get(kernel)
    if record is None:
        return
    header = message['header']
    identity = header.get('msg_id')
    if identity in record['observed']:
        return
    # This bounded set covers both the permanent monitor and browser sockets.
    if len(record['observed']) >= 4096:
        record['observed'].clear()
    record['observed'].add(identity)
    kind = header['msg_type']
    parent = message.get('parent_header', {}).get('msg_id')
    if kind == 'status':
        record['state'] = message['content']['execution_state']
        if record['state'] == 'idle' and parent == record['executing']:
            record['executing'] = None
        record['activity_ms'] = now()
    elif kind in {'stream', 'display_data', 'update_display_data', 'execute_result', 'error'}:
        record['output_bytes'] += size
        record['total_output_bytes'] += size
        record['output_messages'] += 1
        if (record['output_bytes'] > SETTINGS['cell_output_bytes']
                or record['total_output_bytes'] > 10 * 1024 * 1024
                or record['output_messages'] > 2048):
            fail(kernel, 'output_limit')


class GatedKernel(ServerKernelManager):
    def gate(self):
        identity = Path(self.connection_file).stem.removeprefix('kernel-')
        uuid.UUID(identity)
        return json.loads((ROOT / 'kernels' / (identity + '.gate.json')).read_text())

    async def _async_pre_start_kernel(self, **kwargs):
        gate = self.gate()
        kwargs['env'] = dict(os.environ, SUPABRICKS_PROCESS_TOKEN=gate['token'])
        return await super()._async_pre_start_kernel(**kwargs)

    def format_kernel_cmd(self, extra_arguments=None):
        gate = self.gate()
        return [gate['binary'], 'child', '--launch', gate['launch']]


class BoundedChannels(ZMQChannelsWebsocketConnection):
    pending_bytes = 0

    def write_message(self, message, binary=False):
        size = len(message) if isinstance(message, (bytes, str)) else len(json.dumps(message))
        if self.pending_bytes + size > FRAME:
            fail(self.kernel_id, 'output_limit')
            self.websocket_handler.close(1009, 'Notebook output limit')
            return
        self.pending_bytes += size
        try:
            future = super().write_message(message, binary=binary)
        except Exception:
            self.pending_bytes -= size
            raise
        if future is None:
            self.pending_bytes -= size
        else:
            def delivered(done):
                self.pending_bytes -= size
                if not done.cancelled() and done.exception():
                    self.websocket_handler.close()
            future.add_done_callback(delivered)
        return future

    def reject_execution(self, message, name):
        # Standard Jupyter failure and idle replies finish this rejected future;
        # they neither interrupt nor alter an already admitted execution.
        reply = self.session.msg('execute_reply', content={
            'status': 'error', 'ename': name, 'evalue': 'Execution was not submitted',
            'traceback': [], 'execution_count': 0}, parent=message)
        idle = self.session.msg('status', content={'execution_state': 'idle'}, parent=message)
        self.write_message(serialize_msg_to_ws_v1(reply, 'shell', pack=self.session.pack), binary=True)
        self.write_message(serialize_msg_to_ws_v1(idle, 'iopub', pack=self.session.pack), binary=True)

    def handle_incoming_message(self, incoming):
        kernel = self.kernel_id
        record = RECORDS.get(kernel)
        try:
            if not record or not record['ready'] or record.get('error'):
                raise ValueError('unavailable kernel')
            if not isinstance(incoming, bytes) or len(incoming) > FRAME:
                raise ValueError('invalid frame')
            # Check the fixed five-part, no-extra-buffer frame before the
            # upstream decoder allocates a list from an untrusted offset count.
            if len(incoming) < 56 or int.from_bytes(incoming[:8], 'little') != 6:
                raise ValueError('invalid offset count')
            offsets = [int.from_bytes(incoming[8*(i+1):8*(i+2)], 'little') for i in range(6)]
            if offsets[0] != 56 or offsets[-1] != len(incoming) or any(a >= b for a, b in zip(offsets, offsets[1:])):
                raise ValueError('invalid frame offsets')
            channel, parts = deserialize_msg_from_ws_v1(incoming)
            if channel not in {'shell', 'control'} or len(parts) != 4:
                raise ValueError('unsupported channel or buffers')
            message = dict(zip(['header', 'parent_header', 'metadata', 'content'], map(json.loads, parts)))
            identity = message['header']['msg_id']
            if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                raise ValueError('invalid message identity')
            kind = message['header']['msg_type']
            if kind not in {'execute_request', 'kernel_info_request', 'complete_request', 'inspect_request', 'is_complete_request', 'comm_info_request'}:
                raise ValueError('unsupported message')
            second = now() // 1000
            if record['request_second'] != second:
                record['request_second'], record['requests'] = second, 0
            record['requests'] += 1
            if record['requests'] > 32:
                raise ValueError('submission rate')
            if kind == 'execute_request':
                identity = message['header']['msg_id']
                if record['executing'] is not None:
                    self.reject_execution(message, 'ExecutionBusy')
                    return
                if identity in record['executions']:
                    self.reject_execution(message, 'ExecutionAlreadySubmitted')
                    return
                if len(record['executions']) >= 1024:
                    self.reject_execution(message, 'RestartRequired')
                    return
                content = message['content']
                if channel != 'shell' or len(content.get('code', '').encode()) > 65536 or content.get('allow_stdin', True):
                    raise ValueError('execution requires bounded code and allow_stdin=false')
                record['executions'].add(identity)
                record['executing'] = identity
                record['output_bytes'] = record['output_messages'] = 0
                record['activity_ms'] = now()
            super().handle_incoming_message(incoming)
        except Exception:
            fail(kernel, 'protocol_limit')
            self.websocket_handler.close(1008, 'Notebook protocol limit')

    def handle_outgoing_message(self, stream, outgoing):
        kernel = self.kernel_id
        try:
            _, parts = self.session.feed_identities(outgoing)
            size = sum(len(part) for part in parts)
            if size > FRAME:
                fail(kernel, 'output_limit')
                self.websocket_handler.close(1009, 'Notebook output limit')
                return
            message = self.session.deserialize(parts)
            observe(kernel, message, size)
            if RECORDS.get(kernel, {}).get('error'):
                fail(kernel, 'output_limit')
                self.websocket_handler.close(1009, 'Notebook output limit')
                return
            super().handle_outgoing_message(stream, outgoing)
        except Exception:
            fail(kernel, 'protocol_limit')
            self.websocket_handler.close(1008, 'Notebook protocol limit')


async def monitor(kernel, client):
    while kernel in RECORDS:
        try:
            message = await client.get_iopub_msg(timeout=1)
            observe(kernel, message, len(json.dumps(message, default=str)))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # queue.Empty is ordinary when idle; fatal monitor errors fence execution.
            from queue import Empty
            if not isinstance(error, Empty):
                fail(kernel, 'monitor_lost')
                return


async def start(kernel, generation):
    record = dict(generation=generation, ready=False, state='starting', activity_ms=now(),
                  interrupt_ack_ms=0, error=None, executing=None, executions=set(), observed=set(),
                  output_bytes=0, total_output_bytes=0, output_messages=0, request_second=0, requests=0)
    RECORDS[kernel] = record
    publish(kernel)
    try:
        await APP.kernel_manager.start_kernel(kernel_name='supabricks', kernel_id=kernel, path='')
        manager = APP.kernel_manager.get_kernel(kernel)
        if manager.autorestart:
            raise RuntimeError('automatic restart is forbidden')
        client = manager.client()
        client.start_channels()
        await client.wait_for_ready(timeout=60)
        gate = manager.gate()
        if not Path(gate['ready']).is_file():
            raise RuntimeError('Spark bootstrap did not finish')
        record['client'] = client
        record['ready'], record['state'], record['activity_ms'] = True, 'idle', now()
        record['monitor'] = asyncio.create_task(monitor(kernel, client))
        publish(kernel)
    except Exception:
        fail(kernel, 'bootstrap_failed')


async def command(value):
    kernel = str(uuid.UUID(value['kernel_id']))
    if value['action'] == 'start':
        if kernel not in RECORDS:
            await start(kernel, value['generation'])
    elif kernel in RECORDS:
        record = RECORDS[kernel]
        if record['generation'] != value['generation']:
            return
        if value['action'] == 'interrupt':
            await APP.kernel_manager.interrupt_kernel(kernel)
            record['interrupt_ack_ms'] = now()
            publish(kernel)
        elif value['action'] == 'forget':
            if record.get('monitor'):
                record['monitor'].cancel()
            if record.get('client'):
                record['client'].stop_channels()
            if kernel in APP.kernel_manager:
                await APP.kernel_manager.shutdown_kernel(kernel, now=True)
            RECORDS.pop(kernel, None)
            for suffix in ['.context.json', '.ready.json', '.launch.json', '.gate.json', '.fault.json']:
                (ROOT / 'kernels' / (kernel + suffix)).unlink(missing_ok=True)
            (ROOT / 'reports' / (kernel + '.json')).unlink(missing_ok=True)


def tick():
    for path in sorted((ROOT / 'commands').glob('*.json')):
        value = json.loads(path.read_text())
        path.unlink()
        task = asyncio.create_task(command(value))
        TASKS.add(task)
        def finished(done, kernel=value.get('kernel_id')):
            TASKS.discard(done)
            if not done.cancelled() and done.exception():
                fail(kernel, 'control_failed')
        task.add_done_callback(finished)
    for kernel in list(RECORDS):
        publish(kernel)


# Bound native socket queues before any Jupyter/IPython socket is created.
context = zmq.Context.instance()
context.setsockopt(zmq.RCVHWM, 16)
context.setsockopt(zmq.SNDHWM, 16)
context.setsockopt(zmq.MAXMSGSIZE, FRAME)
c = Config()
c.ServerApp.ip = '127.0.0.1'
c.ServerApp.port = SETTINGS['port']
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
c.ServerApp.root_dir = str(ROOT / 'empty-contents')
c.ServerApp.jpserver_extensions = {}
c.ServerApp.terminals_enabled = False
c.ServerApp.allow_remote_access = False
c.ServerApp.allow_origin = ''
c.ServerApp.log_level = 'ERROR'
c.ServerApp.kernel_websocket_connection_class = BoundedChannels
c.ServerApp.tornado_settings = {'websocket_max_message_size': FRAME}
c.IdentityProvider.token = SETTINGS['token']
c.MultiKernelManager.kernel_manager_class = '__main__.GatedKernel'
c.KernelManager.autorestart = False
c.MappingKernelManager.buffer_offline_messages = False
c.MappingKernelManager.connection_dir = str(ROOT / 'runtime')
c.KernelSpecManager.ensure_native_kernel = False
APP = ServerApp.instance(config=c)
APP.initialize([])
spec = ROOT / 'data' / 'kernels' / 'supabricks'
spec.mkdir(parents=True, exist_ok=True)
(spec / 'kernel.json').write_text(json.dumps({'argv': [sys.executable, '-m', 'ipykernel_launcher', '-f', '{connection_file}'], 'display_name': 'Supabricks', 'language': 'python'}))
APP.kernel_spec_manager.kernel_dirs = [str(spec.parent)]
APP.kernel_spec_manager.ensure_native_kernel = False
APP.kernel_manager.kernel_spec_manager = APP.kernel_spec_manager
APP.kernel_manager.connection_dir = str(ROOT / 'runtime')
APP.kernel_manager.kernel_manager_class = '__main__.GatedKernel'
APP.kernel_manager.context.setsockopt(zmq.SNDHWM, 16)
APP.kernel_manager.context.setsockopt(zmq.RCVHWM, 16)
APP.kernel_manager.context.setsockopt(zmq.MAXMSGSIZE, FRAME)
PeriodicCallback(tick, 200).start()
# Jupyter binds during initialize; ready identity is private to the daemon.
atomic(ROOT / 'ready.json', {'pid': os.getpid(), 'port': SETTINGS['port']})
APP.start()
