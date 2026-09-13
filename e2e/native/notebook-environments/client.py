"""Real console session and Jupyter wire client shared by native qualification."""
import http.cookiejar
import json
import struct
import time
import urllib.request
import urllib.error
import uuid
import websocket

class Console:
    def __init__(self,project,cli,channels):
        self.cli=cli;self.channels=channels
        self.created={}
        self.project=project;url=cli(project,'console','--no-open')['url'];self.origin=url.split('/#')[0].rstrip('/')
        self.jar=http.cookiejar.CookieJar();self.http=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf=self.request('session',{'token':url.split('#launch=')[1]})['csrf']
        overview=self.request('overview');b=next(b for b in overview['branches'] if b['name']=='main')
        self.target={'branch':b['id'],'revision':b['revision']}
    def request(self,path,value=None):
        headers={'Origin':self.origin,'X-Supabricks-Console':'1','Content-Type':'application/json'}
        if hasattr(self,'csrf'):headers['X-Supabricks-CSRF']=self.csrf
        req=urllib.request.Request(self.origin+'/api/'+path,headers=headers,data=None if value is None else json.dumps(value).encode())
        try:
            with self.http.open(req,timeout=30) as result:return json.load(result)
        except urllib.error.HTTPError as error:
            # Preserve the bounded API diagnostic; a bare 503 hides whether
            # startup hit a transport deadline or the daemon rejected it.
            detail=error.read(8192).decode('utf-8',errors='replace')
            raise RuntimeError(f'console {path}: HTTP {error.code}: {detail}') from None
    def action(self,action,**fields):return self.request('workspace',{'action':'notebook','command':{'action':action,**fields}})['value']
    def wait(self,e,state='ready'):
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            e=next(item for item in self.action('list') if item['id']==e['id'])
            if e['state']==state:return e
            if e['state'] in ['failed','lost','expired']:raise AssertionError('notebook '+e['state']+': '+str(e.get('error')))
            time.sleep(.15)
        raise TimeoutError('kernel readiness')
    def start(self,environment=None,epoch=None):
        request=dict(key=str(uuid.uuid4()),target=self.target,environment=environment,epoch=epoch)
        e=self.action('create',**request);self.created[e['id']]=request
        e=self.action('start',id=e['id'],generation=0,key=str(uuid.uuid4()))
        return self.wait(e)
    def stop(self,e):
        return self.wait(self.action('shutdown',id=e['id'],generation=e['generation'],key=str(uuid.uuid4())),'stopped')
    def restart(self,e,environment=None):
        fields={} if environment is None else {'environment':environment}
        return self.wait(self.action('restart' if environment is None else 'adopt_environment',id=e['id'],generation=e['generation'],key=str(uuid.uuid4()),**fields))
    def connect(self,e):
        ticket=self.request('notebooks/ticket',{'id':e['id'],'generation':e['generation']})
        ws=websocket.create_connection(self.origin.replace('http:','ws:')+f"/api/notebooks/{e['id']}/{e['generation']}/channels",origin=self.origin,
            cookie='; '.join(c.name+'='+c.value for c in self.jar),subprotocols=[ticket['protocol'],ticket['authorization_protocol']],timeout=30)
        self.channels.append(ws);return ws
def execute(ws,code):
    mid=str(uuid.uuid4());parts=[b'shell',json.dumps({'msg_id':mid,'username':'qualification','session':str(uuid.uuid4()),'msg_type':'execute_request','version':'5.3'}).encode(),b'{}',b'{}',json.dumps(dict(code=code,silent=False,store_history=True,user_expressions={},allow_stdin=False,stop_on_error=True)).encode()]
    offsets=[8*(len(parts)+2)]
    for p in parts:offsets.append(offsets[-1]+len(p))
    ws.send_binary(struct.pack('<'+'Q'*(len(offsets)+1),len(offsets),*offsets)+b''.join(parts))
    reply=None;idle=False;outputs=[]
    while not (reply is not None and idle):
        raw=ws.recv();assert isinstance(raw,bytes)
        count=struct.unpack_from('<Q',raw)[0];assert count<=16
        offsets=struct.unpack_from('<'+'Q'*count,raw,8)
        parts=[raw[a:b] for a,b in zip(offsets,offsets[1:])]
        header,parent,content=(json.loads(parts[i]) for i in [1,2,4])
        if parent.get('msg_id')!=mid:continue
        if header['msg_type']=='execute_reply':reply=content
        elif header['msg_type']=='status' and content['execution_state']=='idle':idle=True
        elif header['msg_type']=='stream':outputs.append(content['text'])
    assert reply['status']=='ok',reply
    return ''.join(outputs)
