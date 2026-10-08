"""Budgeted, lossless timing storage for long SP11 trials (no row payloads)."""
from array import array
from collections.abc import MutableMapping, Sequence
import struct


class PackedSamples(Sequence):
    RECORD = struct.Struct('!Iddd')  # xid, COMMIT ack, transaction time, submission lateness
    FIELDS = ('xid', 'ack_ms', 'latency_ms', 'late_ms')

    def __init__(self, budget=128*1024**2):
        self.data=bytearray();self.order=None;self.budget=budget

    def __len__(self):
        return len(self.data)//self.RECORD.size

    def __getitem__(self,index):
        if isinstance(index,slice):return [self[i] for i in range(*index.indices(len(self)))]
        if index<0:index+=len(self)
        if not 0<=index<len(self):raise IndexError(index)
        if self.order is not None:index=self.order[index]
        return dict(zip(self.FIELDS,self.RECORD.unpack_from(self.data,index*self.RECORD.size)))

    def append(self,sample):
        if self.order is not None:raise ValueError('sorted sample buffer is sealed')
        if set(sample)!=set(self.FIELDS):raise ValueError('unexpected source timing fields')
        if len(self.data)+self.RECORD.size>self.budget:raise ValueError('source timing budget')
        self.data.extend(self.RECORD.pack(*(sample[k] for k in self.FIELDS)))

    def extend(self,other):
        if self.order is not None or other.order is not None:raise ValueError('sorted sample buffer is sealed')
        if len(self.data)+len(other.data)>self.budget:raise ValueError('source timing budget')
        self.data.extend(other.data)

    def sort_by_ack(self):
        # Sort compact indices after input stops; never inflate the full history
        # into one Python dictionary per transaction again.
        self.order=array('I',sorted(range(len(self)),
            key=lambda i:self.RECORD.unpack_from(self.data,i*self.RECORD.size)[1]))
        return self


class PackedMarkers(MutableMapping):
    """Sparse xid pages: bounded even with gaps, out-of-order commits or xid wrap.

    Separate presence bytes retain missing-marker checks, including zero values.
    An xid reused with a different value fails closed rather than misattributing
    transactions on a long-running source.
    """
    PAGE=4096

    def __init__(self,typecode,budget=64*1024**2):
        self.typecode=typecode;self.pages={};self.count=0;self.budget=budget
        self.page_bytes=self.PAGE*(array(typecode).itemsize+1)

    def address(self,key):
        if not isinstance(key,int) or not 0<=key<2**32:raise KeyError(key)
        return divmod(key,self.PAGE)

    def __getitem__(self,key):
        page,offset=self.address(key)
        if page not in self.pages or not self.pages[page][1][offset]:raise KeyError(key)
        return self.pages[page][0][offset]

    def __setitem__(self,key,value):
        page,offset=self.address(key)
        if page not in self.pages:
            if (len(self.pages)+1)*self.page_bytes>self.budget:raise ValueError('capture marker budget')
            self.pages[page]=(array(self.typecode,[0])*self.PAGE,bytearray(self.PAGE))
        values,present=self.pages[page]
        if present[offset] and values[offset]!=value:raise ValueError('ambiguous reused transaction xid')
        values[offset]=value
        if not present[offset]:present[offset]=1;self.count+=1

    def __delitem__(self,key):
        self[key]  # Preserve KeyError semantics.
        page,offset=self.address(key);self.pages[page][1][offset]=0;self.count-=1

    def __iter__(self):
        for page,(_,present) in self.pages.items():
            for offset,found in enumerate(present):
                if found:yield page*self.PAGE+offset

    def __len__(self):return self.count
