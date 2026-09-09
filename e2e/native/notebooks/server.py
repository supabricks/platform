"""Launch private Jupyter with the N01 ownership hook and no ambient extensions."""
import json
import os
from pathlib import Path
from traitlets.config import Config
from jupyter_server.serverapp import ServerApp
from kernel import OwnedKernels

settings = json.loads(Path(os.environ['SB_N01_CONFIG']).read_text())
c = Config()
c.ServerApp.ip = '127.0.0.1'
c.ServerApp.port = settings['jupyter_port']
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
c.ServerApp.base_url = '/jupyter/'
c.ServerApp.root_dir = settings['notebooks']
c.ServerApp.jpserver_extensions = {}
c.ServerApp.kernel_manager_class = OwnedKernels
c.ServerApp.terminals_enabled = False
c.ServerApp.allow_remote_access = False
c.ServerApp.allow_origin = ''
c.ServerApp.log_level = 'ERROR'
c.IdentityProvider.token = settings['jupyter_token']
c.KernelSpecManager.ensure_native_kernel = False
c.KernelManager.autorestart = False
c.MappingKernelManager.default_kernel_name = 'supabricks-probe'
c.MappingKernelManager.cull_idle_timeout = 300
c.ServerApp.tornado_settings = {'websocket_max_message_size': 2 * 1024 * 1024}
app = ServerApp.instance(config=c)
app.initialize([])
app.kernel_spec_manager.kernel_dirs = [settings['kernels']]
app.kernel_spec_manager.ensure_native_kernel = False
app.kernel_manager.kernel_spec_manager = app.kernel_spec_manager
app.start()
