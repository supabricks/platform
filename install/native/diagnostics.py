"""Bounded structural failure evidence; never export log messages or notebook data."""
import re
from pathlib import Path


def summarize(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink(): return {'available':False}
    size=path.stat().st_size
    with path.open('rb') as f:
        f.seek(max(0,size-32768))
        text=f.read(32768).decode('utf-8',errors='replace')
    frames=[dict(file=Path(file).name,line=int(line),function=function)
        for file,line,function in re.findall(r'^\s*File "([^"\n]+)", line (\d{1,8}), in ([A-Za-z_][A-Za-z_0-9]*)\s*$',text,re.M)][-8:]
    # Only recognize predefined failures. Unknown class names can contain data.
    known=('AssertionError','TimeoutError','RuntimeError','CalledProcessError',
           'WebSocketTimeoutException','ConnectionRefusedError','FileNotFoundError',
           'SparkConnectGrpcException','AnalysisException','PermissionError')
    return dict(available=True,bytes=size,truncated=size>32768,frames=frames,
                failure_types=[name for name in known if re.search(r'\b'+name+r'\b',text)])
