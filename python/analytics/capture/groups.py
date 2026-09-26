"""Bounded complete-transaction accumulation; only Spool owns durable progress."""
import time
from .spool import CaptureError, MAX_TRANSACTION, MAX_GROUP_TRANSACTIONS, frames


class Groups:
    def __init__(self, spool, count=32, byte_limit=1024*1024, age=.010, clock=time.monotonic):
        if not 1<=count<=MAX_GROUP_TRANSACTIONS or not 1<=byte_limit<=MAX_TRANSACTION or not 0<=age<=.025:
            raise CaptureError('invalid_group_limits')
        self.spool=spool;self.count=count;self.byte_limit=byte_limit;self.age=age;self.clock=clock
        self.pending=[];self.bytes=0;self.started=None;self.decoded=spool.captured
        self.stats=dict(groups=0,transactions=0,payload_bytes=0,maximum_transactions=0,maximum_bytes=0,
                        accumulation_ms=0,maximum_accumulation_ms=0,commit_ms=0)

    def wait(self, maximum=.2):
        return maximum if not self.pending else min(maximum,max(0,self.started+self.age-self.clock()))

    def before(self, tx):
        return bool(self.pending and (len(self.pending)>=self.count or self.bytes+len(tx[2])>self.byte_limit or self.wait()==0))

    def add(self, tx):
        if len(tx[2])>MAX_TRANSACTION:raise CaptureError('transaction_budget')
        # The age can expire after the caller's before() check. That requests
        # an immediate flush below; only hard capacity limits reject admission.
        if self.pending and (len(self.pending)>=self.count or self.bytes+len(tx[2])>self.byte_limit):
            raise CaptureError('group_budget')
        if not self.pending:self.started=self.clock()
        self.pending.append(tx);self.bytes+=len(tx[2]);self.decoded=max(self.decoded,tx[1])
        # A supported oversized transaction is isolated, never split. Any
        # transactional barrier closes its group immediately.
        barrier=self.spool.identity.get('decoder_version')==2 and any(f[:1]==b'M' for f in frames(tx[2]))
        return len(self.pending)>=self.count or self.bytes>=self.byte_limit or self.wait()==0 or barrier

    def flush(self):
        if not self.pending:return None
        started=self.clock();age_ms=(started-self.started)*1000
        result=self.spool.append_many(self.pending)
        commit_ms=(self.clock()-started)*1000
        if result['transactions']:
            self.stats['groups']+=1
            for key in ('transactions','payload_bytes'):self.stats[key]+=result[key]
            self.stats['maximum_transactions']=max(self.stats['maximum_transactions'],result['transactions'])
            self.stats['maximum_bytes']=max(self.stats['maximum_bytes'],result['payload_bytes'])
            self.stats['accumulation_ms']+=age_ms
            self.stats['maximum_accumulation_ms']=max(self.stats['maximum_accumulation_ms'],age_ms)
            self.stats['commit_ms']+=commit_ms
        self.pending=[];self.bytes=0;self.started=None
        return dict(result,accumulation_ms=age_ms,commit_ms=commit_ms)

    def progress(self):
        return dict(self.stats,pending_transactions=len(self.pending),pending_bytes=self.bytes,
                    decoded_lsn=self.decoded,durable_lsn=self.spool.captured,
                    limits=dict(transactions=self.count,payload_bytes=self.byte_limit,accumulation_ms=self.age*1000))
