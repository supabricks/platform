"""One ephemeral decode job for an already authorized, bounded journal range.

This stage cannot read Delta, mutate storage, acknowledge WAL or publish. Its
thread overlaps Python decoding with native/storage work that releases the GIL;
it does not make two Python CPU stages run on separate cores. No job or decoded
state survives a request, including failure and mailbox reuse.
"""
from dataclasses import dataclass
import threading
import time
from capture.spool import CaptureError, lsn
from .rows import changes, MAX_ROWS

# Aggregate admission is independent of per-transaction MAX_ROWS, 16-MiB
# journal reads, 32-MiB Arrow values and 64-MiB sealed plans.
LARGE_APPLY_ROWS=65536


def row_limit(config):
    return LARGE_APPLY_ROWS if config.get('storage_profile','compact')=='large' else MAX_ROWS


@dataclass
class DecodedBatch:
    schema: dict
    operations: list
    end: int
    input_bytes: int


def decode(config, journal_data, check, *, remaining_rows=None, remaining_bytes=None):
    schema,transactions,_,_=journal_data
    limit=row_limit(config) if remaining_rows is None else min(row_limit(config),remaining_rows)
    operations=[];end=lsn(config['after_lsn']);input_bytes=0
    prefix=('supabricks.barrier.'+config['identity']['generation']
            if config['identity'].get('decoder_version')==2 else None)
    check()
    for candidate_end,payload in transactions:
        check()
        if len(operations)>=limit:break
        if remaining_bytes is not None and input_bytes+len(payload)>remaining_bytes:break
        selected=changes(payload,schema,candidate_end,prefix)
        # Per-transaction admission stays in changes(). Never split a commit to
        # fill the aggregate batch; the next authorized run handles the remainder.
        if len(operations)+len(selected)>limit:break
        operations.extend(selected);end=candidate_end;input_bytes+=len(payload)
    check()
    return DecodedBatch(schema,operations,end,input_bytes)


class Preparation:
    def __init__(self, config, journal_data):
        self.config=config;self.data=journal_data
        self.cancelled=threading.Event();self.thread=None
        self.value=None;self.error=None

    def check(self):
        if self.cancelled.is_set():raise CaptureError('apply_preparation_cancelled')
        if time.time()*1000>=self.config['deadline_ms']:raise CaptureError('apply_deadline')

    def work(self):
        try:self.value=decode(self.config,self.data,self.check)
        except BaseException as error:self.error=error
        finally:self.data=None

    def __enter__(self):
        self.check()
        self.thread=threading.Thread(target=self.work,name='apply-prepare')
        self.thread.start()
        return self

    def result(self):
        self.thread.join()
        self.check()
        if self.error is not None:raise self.error
        value=self.value;self.value=None
        if value is None:raise CaptureError('apply_preparation_consumed')
        return value

    def __exit__(self, *unused):
        # Decoding is bounded by the existing 4-MiB/16,384-row transaction
        # limits; cancellation is checked between transactions. Never return to
        # the reusable mailbox with a live preparation thread or retained rows.
        self.cancelled.set()
        self.thread.join()
        self.value=None;self.error=None;self.data=None;self.config=None
