"""Enter qualification isolation after preparing an immutable historical fixture."""
import ctypes
import errno
import ipaddress
import json
from pathlib import Path
import subprocess
import sys


def enter(path):
    if sys.platform != 'darwin':
        raise RuntimeError('macOS qualification policy requires macOS')
    # Applying Seatbelt does not revoke pre-opened sockets. The harness may
    # retain only its localhost installer and predecessor notebook channels.
    import psutil
    for connection in psutil.Process().net_connections(kind='inet'):
        if connection.raddr:
            address = ipaddress.ip_address(connection.raddr.ip)
            assert address.is_loopback or getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback
    policy = Path(path).read_bytes()
    library = ctypes.CDLL('/usr/lib/libsandbox.dylib')
    library.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
    library.sandbox_init.restype = ctypes.c_int
    library.sandbox_free_error.argtypes = [ctypes.c_char_p]
    library.sandbox_free_error.restype = None
    error = ctypes.c_char_p()
    if library.sandbox_init(policy, 0, ctypes.byref(error)) != 0:
        if error.value:
            library.sandbox_free_error(error)
        raise RuntimeError('could not enter macOS qualification policy')
    # Prove a new child inherits the outbound denial before invoking candidate
    # installer/upgrade code. Never classify a route failure as policy denial.
    probe = '''import json,socket
with socket.socket() as s:
    s.settimeout(2)
    try:s.connect(('1.1.1.1',443));value=None
    except OSError as e:value=e.errno
print(json.dumps(value))
'''
    result = json.loads(subprocess.check_output([sys.executable, '-I', '-B', '-c', probe], text=True, timeout=10))
    assert result == errno.EPERM, 'candidate child external TCP was not denied by policy'
    return dict(predecessor_preparation='outside_network_qualification',
                candidate_policy_entered=True, child_external_tcp_errno=result,
                inherited_external_connections=0)
