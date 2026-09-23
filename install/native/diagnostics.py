"""Bounded structural failure evidence; never export log messages or notebook data."""
import ast
import json
import re
from pathlib import Path


def summarize(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink(): return {'available':False}
    size=path.stat().st_size
    with path.open('rb') as f:
        f.seek(max(0,size-32768))
        text=f.read(32768).decode('utf-8',errors='replace')
    files={'client.py','pk05-notebook.py','qualify_catalog.py','catalog_gate.py',
           'service.py','recovery.py','datasets.py','publication.py','reads.py',
           'metadata.py','cell.py','epochs.py','project_offline.py',
           'qualify.py','qualify_governed.py','governed_upgrade.py','qualify_sync.py',
           'installed_sync.py','continuous.py','sync_maintenance.py','sync_surfaces.py'}
    functions={'execute','main','qualify','run','wait','start','connect','request','stop','workload','observe','process_samples'}
    frames=[dict(file=Path(file).name if Path(file).name in files else 'runtime',
                 line=int(line),function=function if function in functions else 'runtime')
        for file,line,function in re.findall(r'^\s*File "([^"\n]+)", line (\d{1,8}), in ([A-Za-z_][A-Za-z_0-9]*)\s*$',text,re.M)][-8:]
    # Only recognize predefined failures. Unknown class names can contain data.
    known=('AssertionError','TimeoutError','RuntimeError','ConsoleHTTPError','CalledProcessError',
           'WebSocketTimeoutException','ConnectionRefusedError','FileNotFoundError',
           'SparkConnectGrpcException','AnalysisException','PermissionError')
    api=[]
    codes=('invalid_input','not_found','conflict','unavailable','sql_error','io_error','internal')
    for status,raw in re.findall(r'console [a-z_/]+: HTTP (\d{3}): (\{[^\n]+\})',text):
        try:
            error=json.loads(raw).get('error',{})
            if not isinstance(error,dict):continue
        except (ValueError,TypeError):continue
        value=dict(status=int(status),code=error.get('code') if error.get('code') in codes else 'unknown')
        if isinstance(error.get('retryable'),bool):value['retryable']=error['retryable']
        # Map only known OS diagnostics; never export the free-form message.
        message=error.get('message','')
        if isinstance(message,str):
            if re.search(r'Resource temporarily unavailable \(os error (11|35)\)',message):value['io_kind']='would_block'
            elif 'timed out' in message.lower():value['io_kind']='timed_out'
        api.append(value)
    native=[]
    pg_states={'connection','08001','08006','28P01','28000','40001','40P01','42501','42P01','55P03','57014','57P01'}
    for raw in re.findall(r'^RuntimeError: (\{[^\n]+\})$',text,re.M):
        try:error=ast.literal_eval(raw)
        except (ValueError,SyntaxError):continue
        if not isinstance(error,dict):continue
        value=dict(code=error.get('code') if error.get('code') in codes else 'unknown')
        if isinstance(error.get('retryable'),bool):value['retryable']=error['retryable']
        message=error.get('message','')
        if isinstance(message,str):
            match=re.fullmatch(r'governed PostgreSQL rejected request \(([A-Z0-9]{5}|connection)\)',message)
            if match:value['postgres_state']=match[1] if match[1] in pg_states else 'other'
            if message=='governed data action is not authorized or its branch profile is unsupported':value['reason']='governed_denied'
        native.append(value)
    return dict(available=True,bytes=size,truncated=size>32768,frames=frames,console_errors=api[-4:],native_errors=native[-4:],
                readiness_poll_503_observed=len(re.findall(r'^NOTEBOOK_READINESS_TRANSIENT_503$',text,re.M)),
                failure_types=[name for name in known if re.search(r'\b'+name+r'\b',text)])
