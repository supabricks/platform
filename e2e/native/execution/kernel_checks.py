import os
import socket
import subprocess
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
from deltalake import DeltaTable
assert os.getuid() == 1000
assert 'SUPABRICKS_UC094_CONFIG' not in os.environ
assert 'SUPABRICKS_UC093_RUNTIME' not in os.environ
for path in (HOST_CANARY_JSON, HOST_SOCKET_JSON, '/proc/HOST_PROCESS_PID/environ'):
    try:
        with open(path,'rb') as stream: stream.read(1)
    except OSError: pass
    else: raise AssertionError('host canary crossed boundary')
try: socket.create_connection(('127.0.0.1', HOST_UC_PORT),timeout=.2).close()
except OSError: pass
else: raise AssertionError('host UC listener reachable')
client = socket.socket(socket.AF_UNIX)
try: client.connect(HOST_SOCKET_JSON)
except OSError: pass
else: raise AssertionError('host control socket reachable')
finally: client.close()

assert pd.Series(np.arange(100)).sum() == 4950
assert pa.array([1,2]).to_pylist() == [1,2]
table = '/admission/data/TABLE_UUID'
assert DeltaTable(table).to_pyarrow_table()['id'].to_pylist() == [1,2,3]
assert spark.sql(f'SELECT sum(id) AS n FROM delta.`{table}`').collect()[0].n == 6
for path in ('/var/run/docker.sock','/tools/runsc','/rootfs.tar','/work/config.json','/etc/shadow','/host/data/state.sqlite3','/host-secrets/credential'):
    try:
        with open(path,'rb') as f: f.read(1)
    except (OSError,PermissionError): pass
    else: raise AssertionError(path)
for host,port in [('127.0.0.1',5432),('127.0.0.1',8080),('172.17.0.1',5432),('1.1.1.1',443)]:
    try: socket.create_connection((host,port),timeout=.2).close()
    except OSError: pass
    else: raise AssertionError('external network')
try: socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)
except PermissionError: pass
else: raise AssertionError('raw socket')
try: os.setuid(0)
except PermissionError: pass
else: raise AssertionError('setuid')
assert not any(k in os.environ for k in ('AWS_SECRET_ACCESS_KEY','PGPASSWORD','DOCKER_HOST','UC_TOKEN','SUPABRICKS_PROCESS_TOKEN'))
for path in ('/product/poison','/admission/source.json',table+'/_delta_log/00000000000000000000.json'):
    try: Path(path).write_text('poison')
    except OSError: pass
    else: raise AssertionError('read-only admission')
assert os.statvfs('/scratch').f_blocks*os.statvfs('/scratch').f_frsize == 512*1024*1024
with open('/scratch/quota','wb') as f:
    try: os.posix_fallocate(f.fileno(),0,513*1024*1024)
    except OSError as e: assert e.errno == 28
    else: raise AssertionError('scratch quota')
Path('/scratch/quota').unlink()
# Parser and executable package preparation run in the same untrusted boundary.
# No prepared artifact is activated outside the sandbox (UC09.5).
Path('/scratch/input.csv').write_text('id,name\n1,Alice\n2,Bob\n')
assert pd.read_csv('/scratch/input.csv').id.sum() == 3
Path('/scratch/setup.py').write_text("from setuptools import setup;setup(name='isolated_fixture',version='1.0',py_modules=['example'])")
Path('/scratch/example.py').write_text('value=42\n')
subprocess.run(['/scratch/env/bin/python','setup.py','build'],cwd='/scratch',check=True,stdout=subprocess.DEVNULL)
assert Path('/scratch/build/lib/example.py').read_text() == 'value=42\n'
Path('/scratch/private-cache').write_text('alice only')
# Exhaust a lowered child process limit; the enclosing lease still has the
# fixed 256-process guest limit and 512-task cgroup, and other leases survive.
import resource
assert resource.getrlimit(resource.RLIMIT_NPROC) == (256,256)
assert resource.getrlimit(resource.RLIMIT_NOFILE) == (256,256)
fork_check = '''import os,resource,signal,time
resource.setrlimit(resource.RLIMIT_NPROC,(32,32))
children=[]
denied=False
try:
 for _ in range(64):
  try: pid=os.fork()
  except OSError:
   denied=True;break
  if pid==0:
   time.sleep(10);os._exit(0)
  children.append(pid)
finally:
 for pid in children:
  os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
assert denied
'''
subprocess.run(['/scratch/env/bin/python','-S','-c',fork_check],check=True,timeout=20)
# File-descriptor exhaustion is contained to this subprocess too.
fd_check = '''import os
files=[]
try:
 for _ in range(1024): files.append(os.open('/dev/null',os.O_RDONLY))
except OSError as e: assert e.errno==24
else: raise AssertionError('descriptor limit')
finally:
 for fd in files: os.close(fd)
'''
subprocess.run(['/scratch/env/bin/python','-S','-c',fd_check],check=True,timeout=10)

print('UC094_CHECKS_PASSED',flush=True)
