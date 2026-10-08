"""Diagnostic-only backend-neutral transaction markers. Never feedback authority.

Installed identically into every SP10c measured arm. Opt-in before capture starts;
records are written only AFTER the durable backend call succeeds. No row values,
no new fsync, bounded output. Loss/truncation/missing markers invalidate the trial.
"""
import os
from pathlib import Path
import struct
import time

RECORD=struct.Struct('!QQId')  # sequence, end LSN, source xid, observed wall ms
LIMIT=256*1024*1024


def install(namespace):
    cls=namespace['Spool'];original_open=cls.open;original_append=cls.append_many;original_close=cls.close
    def open(self,*args,**kwargs):
        self._marker_fd=None
        original_open(self,*args,**kwargs)
        root=self.root.parents[2]/'marker-observer'
        if not (root/'enabled').is_file():return
        path=root/(self.root.parent.name+'.bin')
        self._marker_fd=os.open(path,os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
        size=os.fstat(self._marker_fd).st_size
        if size>LIMIT or size%RECORD.size:raise RuntimeError('invalid_marker_file')
        self._marker_seq=size//RECORD.size
    def append(self,transactions):
        previous=self.captured;result=original_append(self,transactions)
        if self._marker_fd is not None and result['transactions']:
            stamp=time.time()*1000;data=bytearray()
            for commit,end,payload in transactions:
                if end<=previous:continue
                if len(payload)<25:raise RuntimeError('invalid_marker_payload')
                self._marker_seq+=1
                data.extend(RECORD.pack(self._marker_seq,end,struct.unpack('!I',payload[21:25])[0],stamp))
            if self._marker_seq*RECORD.size>LIMIT:raise RuntimeError('marker_budget')
            view=memoryview(data)
            while view:
                count=os.write(self._marker_fd,view)
                if not count:raise RuntimeError('marker_write_incomplete')
                view=view[count:]
        return result
    def close(self):
        fd=getattr(self,'_marker_fd',None)
        if fd is not None:os.close(fd);self._marker_fd=None
        return original_close(self)
    cls.open=open;cls.append_many=append;cls.close=close
