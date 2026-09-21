"""Adversarial checks and bounded data reads, executed by the managed ipykernel."""
import json
import os
from pathlib import Path
import socket
import time
from IPython import get_ipython
spark = get_ipython().user_ns["spark"]
import numpy as np
import pandas as pd
import pyarrow as pa
import deltalake
import grpc
import zmq

configuration = json.loads(Path('/probe-config.json').read_text())
import psutil
checks = []
metrics = {'idle_process_rss_bytes': sum(p.memory_info().rss for p in psutil.process_iter())}

def check(name, condition):
    assert condition, name
    checks.append(name)

check('native_numpy_pandas_arrow', int(pd.Series(np.arange(100)).sum()) == 4950 and pa.array([1,2]).to_pylist() == [1,2])
check('python_user_is_unprivileged', os.getuid() == 1000)
try: os.setuid(0)
except PermissionError: denied=True
else: denied=False
check('cannot_elevate_to_root',denied)
try: raw=socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)
except PermissionError: denied=True
else: raw.close(); denied=False
check('raw_network_socket_denied',denied)
check('own_admitted_data_readable', Path('/admitted/owner').read_text() == configuration['principal'])
for path in configuration['forbidden_paths']:
    try:
        with open(path,'rb') as stream: stream.read(1)
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        denied = True
    else: denied = False
    check('filesystem_denied:' + path, denied)
for path in ('/var/run/docker.sock', '/host-secrets/control.sock', '/run/runsc/alice_control.sock'):
    client = socket.socket(socket.AF_UNIX); client.settimeout(.5)
    try: client.connect(path)
    except OSError: denied = True
    else: denied = False
    finally: client.close()
    check('unix_socket_denied:' + path, denied)
for host, port in configuration['forbidden_tcp']:
    try:
        with socket.create_connection((host, port), timeout=.5): pass
    except OSError: denied = True
    else: denied = False
    check(f'tcp_denied:{host}:{port}', denied)
check('no_host_secret_environment', 'IAM00_HOST_SECRET' not in os.environ)
processes = []
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try: processes.append(path.read_bytes())
    except OSError: pass
check('no_other_principal_processes', not any(b'IAM00_HOST_CANARY' in p for p in processes))
try: Path('/product/iam00-write-test').write_text('bad')
except OSError: denied = True
else: denied = False
check('product_mount_is_readonly', denied)
check('scratch_quota_is_512m', os.statvfs('/scratch').f_blocks*os.statvfs('/scratch').f_frsize == 512*1024*1024)
# Reserve beyond the actual tmpfs capacity without writing hundreds of MB.
with open('/scratch/quota-test','wb') as quota:
    try: os.posix_fallocate(quota.fileno(),0,513*1024*1024)
    except OSError as error: quota_denied = error.errno == 28
    else: quota_denied = False
Path('/scratch/quota-test').unlink()
check('scratch_over_quota_denied', quota_denied)
try:
    spark.sql('SELECT * FROM delta.`/other-data/private`').collect()
except Exception: denied = True
else: denied = False
check('sail_raw_unadmitted_delta_denied', denied)
for mb in (10,100):
    start = time.monotonic()
    rows = spark.sql(f'SELECT count(*) AS n, sum(length(payload)) AS bytes FROM delta.`/admitted/data{mb}`').collect()
    metrics[f'delta_{mb}mb_read_seconds'] = round(time.monotonic() - start,3)
    check(f'sail_delta_{mb}mb_full_payload', rows[0]['bytes'] == mb * 1024 * 1024)
    check(f'native_deltalake_{mb}mb', deltalake.DeltaTable(f'/admitted/data{mb}').to_pyarrow_table().num_rows == mb*1024)
Path('/scratch/private-cache').write_text('private cached result')
Path('/scratch/private-credential').write_text('fixture credential, not a host token')
Path('/scratch/kernel-pid.json').write_text(json.dumps(dict(pid=os.getpid())))
Path('/scratch/kernel-results.json').write_text(json.dumps(dict(checks=checks, metrics=metrics,
    packages=dict(numpy=np.__version__,pandas=pd.__version__,pyarrow=pa.__version__,grpc=grpc.__version__,zmq=zmq.__version__))))
