import json,socket,subprocess,sys
from pathlib import Path
root=Path('build');root.mkdir(exist_ok=True)
base=Path('install/native/offline-macos.sb').read_text()
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(32)
port=listener.getsockname()[1]
script=root/'mapped-client.py'
script.write_text('''import socket,json,sys
report={}
for name,family,address in [('ipv4',socket.AF_INET,('127.0.0.1',int(sys.argv[1]))),('mapped',socket.AF_INET6,('::ffff:127.0.0.1',int(sys.argv[1]))),('external',socket.AF_INET,('1.1.1.1',443))]:
    with socket.socket(family,socket.SOCK_STREAM) as s:
        s.settimeout(2)
        try:s.connect(address);report[name]='connected'
        except OSError as e:report[name]=e.errno
print(json.dumps(report))
''')
reports={}
for label,rule in [('strict',''),('mapped','(allow network-outbound (remote ip "::ffff:127.0.0.1:*"))'),('bracketed','(allow network-outbound (remote ip "[::ffff:127.0.0.1]:*"))'),('numeric','(allow network-outbound (remote ip "127.0.0.1:*"))'),('tcp4','(allow network-outbound (remote tcp4 "127.0.0.1:*"))'),('tcp6','(allow network-outbound (remote tcp6 "[::ffff:127.0.0.1]:*"))'),('ip6','(allow network-outbound (remote ip6 "::ffff:127.0.0.1:*"))')]:
    p=subprocess.run(['sandbox-exec','-p',base+'\n'+rule,sys.executable,str(script),str(port)],capture_output=True,text=True,timeout=20)
    reports[label]=dict(exit_code=p.returncode)
    if p.returncode==0:reports[label]['connections']=json.loads(p.stdout)
    else:reports[label]['parser_error']=p.stderr[:1024]
(root/'mapped.json').write_text(json.dumps(reports,indent=2));print(json.dumps(reports,indent=2))
listener.close()
