"""Journal read policy shared by direct qualification and the private owner."""
import hashlib
import time
from .spool import CaptureError, lsn, checked_prefix, MAX_TRANSACTION, MAX_BATCH


def read_range(view, config, deadline, check=lambda:None, max_records=None):
    meta=view.metadata()
    if meta['identity']!=config['identity'] or meta['bootstrap']['lsn']!=config['bootstrap_lsn']:
        raise CaptureError('spool_identity_mismatch')
    after=lsn(config['after_lsn']);target=lsn(config['target_lsn'])
    prefix=checked_prefix(meta.get('pruned_prefix'),meta['identity'],meta['start'],meta['captured'])
    if meta['captured']<target or prefix['lsn']>after:raise CaptureError('source_history_lost')
    result=[];used=0;previous=None;end=after
    for cut,prior,commit,size,payload,checksum in view.records(f'{after:016x}',f'{target:016x}'):
        check()
        cut,prior,commit=int(cut,16),int(prior,16),int(commit,16)
        if size>MAX_TRANSACTION or len(payload)!=size or hashlib.sha256(payload).hexdigest()!=checksum:
            raise CaptureError('spool_corrupt')
        if not prior<=commit<cut or (previous is not None and prior!=previous) or (previous is None and (prior>after or (after!=lsn(config['bootstrap_lsn']) and prior!=after))):
            raise CaptureError('spool_history_gap')
        if used+size>MAX_BATCH or (max_records is not None and len(result)>=max_records):break
        used+=size;result.append((cut,payload));previous=end=cut
    if not result and target>after:raise CaptureError('spool_history_gap')
    check()
    if time.monotonic()>=deadline:raise CaptureError('journal_read_deadline')
    return meta['schema'],result,end,used
