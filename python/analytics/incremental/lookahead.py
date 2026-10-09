"""Ephemeral, bounded, typed decode transfer. No Delta or publication authority.

JSON lines avoid an additional whole-batch JSON object/encoded copy. Never use
pickle: the consumer validates the type/shape of each row before using it.
"""
from datetime import date
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import resource
import sys
import time
from capture.owner import private_directory, private_json
from capture.spool import CaptureError, atomic, canonical, lsn, pg_lsn
from .preparation import DecodedBatch, decode, row_limit
from .rows import UNCHANGED, key_columns, key_values, row_key, value
from .storage import journal

MAX_BYTES=64*1024*1024
MAX_LINE=2*1024*1024
SCOPE=('identity','worker_generation','source_revision','bootstrap_lsn','storage_profile','journal_access')


def check(config):
    if time.time()*1000>=config['deadline_ms']:raise CaptureError('apply_deadline')


def hint(config, decoded):
    if not config.get('prepare_next'):return
    check(config)
    atomic(Path(config['workspace'])/'decoded.json',dict(id=config['id'],attempt=config['attempt'],
        worker_generation=config['worker_generation'],end_lsn=pg_lsn(decoded.end),
        schema_sha256=hashlib.sha256(canonical(decoded.schema)).hexdigest()))


def scalar(v):
    if v is UNCHANGED:return {'unchanged':True}
    if isinstance(v,(Decimal,date)):return str(v)
    return v


def prepare(config):
    # No direct backend fallback is allowed for this separately authorized job.
    if config.get('preparation')!=1 or 'journal_access' not in config:raise CaptureError('preparation_authority')
    check(config);wall=time.monotonic();cpu=time.process_time();started=int(time.time()*1000)
    data=journal(config)
    decoded=decode(config,data,lambda:check(config));del data
    if hashlib.sha256(canonical(decoded.schema)).hexdigest()!=config['schema_sha256']:
        raise CaptureError('schema_changed')
    work=Path(config['workspace']);size=0;digest=hashlib.sha256()
    with (work/'batch.jsonl').open('xb') as out:
        def line(v):
            nonlocal size
            check(config);encoded=canonical(v)+b'\n';size+=len(encoded)
            if len(encoded)>MAX_LINE or size>MAX_BYTES:raise CaptureError('preparation_byte_budget')
            out.write(encoded);digest.update(encoded)
        line(decoded.schema)
        for oid,tag,old,new,row in decoded.operations:
            line([oid,tag.decode('ascii'),old,new,None if row is None else [scalar(v) for v in row]])
    # Ephemeral data need not be fsynced: restart discards it. Publish the small
    # receipt only after close; the daemon additionally waits for process exit.
    atomic(work/'result.json',dict(state='ready',id=config['id'],end_lsn=pg_lsn(decoded.end),
        input_bytes=decoded.input_bytes,rows=len(decoded.operations),bytes=size,sha256=digest.hexdigest(),
        journal_read=config['_journal_read'],elapsed_ms=round((time.monotonic()-wall)*1000),
        cpu_ms=round((time.process_time()-cpu)*1000),started_at_ms=started,prepared_at_ms=int(time.time()*1000),
        peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024)))


def consume(config):
    attachment=config.get('prepared_batch')
    if attachment is None:return None
    check(config);started=time.monotonic()
    issued=attachment['authorization'];receipt=attachment['receipt']
    if (issued.get('preparation')!=1 or any(issued[k]!=config[k] for k in SCOPE)
        or issued['after_lsn']!=config['after_lsn']
        or issued['epoch_id']!=config['previous']['epoch_id']
        or config['previous']['manifest']['source']['lsn']!=config['after_lsn']
        or receipt['id']!=issued['id'] or receipt['state']!='ready'
        or not lsn(config['after_lsn'])<lsn(receipt['end_lsn'])<=lsn(issued['target_lsn'])<=lsn(config['target_lsn'])):
        raise CaptureError('preparation_fenced')
    check(issued)
    work=Path(config['workspace'])/'prepared'
    private_directory(work)
    if private_json(work/'input.json',4*1024*1024)!=issued:raise CaptureError('preparation_fenced')
    rows=receipt['rows'];size=receipt['bytes']
    if type(rows) is not int or not 0<=rows<=row_limit(config) or type(size) is not int or not 0<size<=MAX_BYTES:
        raise CaptureError('preparation_byte_budget')
    fd=os.open(work/'batch.jsonl',os.O_RDONLY|os.O_NOFOLLOW)
    operations=[];digest=hashlib.sha256();used=0
    with os.fdopen(fd,'rb') as stream:
        meta=os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1 or meta.st_uid!=os.getuid() or meta.st_mode&0o077 or meta.st_size!=size:
            raise CaptureError('preparation_fenced')
        def line():
            nonlocal used
            check(config);raw=stream.readline(MAX_LINE+1);used+=len(raw)
            if len(raw)>MAX_LINE or used>size or not raw.endswith(b'\n'):raise CaptureError('preparation_byte_budget')
            digest.update(raw);return json.loads(raw)
        schema=line()
        if hashlib.sha256(canonical(schema)).hexdigest()!=issued['schema_sha256']:raise CaptureError('schema_changed')
        tables=config['previous']['manifest']['tables']
        if set(schema)!={str(t['oid']) for t in tables}:raise CaptureError('schema_changed')
        for t in tables:
            s=schema[str(t['oid'])]
            if (s[:2]!=[t['schema'],t['name']] or
                [c[1:] for c in s[3]]!=[[c['name'],c['type_oid'],c['typmod']] for c in t['columns']]):raise CaptureError('schema_changed')
        for _ in range(rows):
            oid,tag,old,new,row=line()
            if oid not in schema or tag not in ('I','U','D'):raise CaptureError('preparation_shape')
            columns=schema[oid][3];keys=key_columns(columns)
            def key(k):
                v=key_values(k,keys);return v[0] if len(v)==1 else tuple(v)
            old=key(old)
            if tag=='D':
                if row is not None or new is not None:raise CaptureError('preparation_shape')
            else:
                new=key(new)
                if not isinstance(row,list) or len(row)!=len(columns):raise CaptureError('preparation_shape')
                converted=[]
                for v,c in zip(row,columns):
                    if isinstance(v,dict):
                        if tag!='U' or v!={'unchanged':True}:raise CaptureError('preparation_shape')
                        converted.append(UNCHANGED)
                    elif v is None:converted.append(None)
                    elif type(v) in (str,int):converted.append(value(v,c))
                    else:raise CaptureError('preparation_shape')
                row=converted
                if row_key(row,keys)!=new or (tag=='I' and old!=new):raise CaptureError('preparation_shape')
            operations.append((oid,tag.encode('ascii'),old,new,row))
        if stream.read(1) or used!=size or digest.hexdigest()!=receipt['sha256']:raise CaptureError('preparation_checksum')
    if type(receipt['input_bytes']) is not int or not 0<=receipt['input_bytes']<=16*1024*1024:raise CaptureError('preparation_byte_budget')
    config['_journal_read']=receipt['journal_read']
    config['_preparation']=dict(outcome='consumed',rows=rows,bytes=size,prepare_ms=receipt['elapsed_ms'],prepare_cpu_ms=receipt['cpu_ms'],
        consume_ms=round((time.monotonic()-started)*1000),age_ms=attachment.get('age_ms'),
        started_at_ms=receipt['started_at_ms'],prepared_at_ms=receipt['prepared_at_ms'],peak_rss_bytes=receipt['peak_rss_bytes'])
    return DecodedBatch(schema,operations,lsn(receipt['end_lsn']),receipt['input_bytes'])


def optional_consume(config):
    try:return consume(config)
    except (CaptureError,OSError,ValueError,KeyError,TypeError,OverflowError):
        # A stale/missing result is a cache miss before initialization/mutation.
        config['_preparation']={'outcome':'discarded'}
        return None
