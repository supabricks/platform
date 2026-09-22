"""Strict pgoutput row conversion and transaction-ordered primary-key overlay."""
from decimal import Decimal, InvalidOperation
import struct
from capture.spool import CaptureError, frames, MAX_MESSAGE
from capture.protocol import Reader

UNCHANGED = object()
MAX_ROWS = 16384
MAX_VALUES = 32*1024*1024


def value(raw, column):
    if raw is None:return None
    typ,mod=column[2:]
    try:
        if typ in (20,21,23):
            n=int(raw);bits={20:64,21:16,23:32}[typ]
            if not -(1<<(bits-1))<=n<(1<<(bits-1)):raise ValueError()
            return n
        if typ==1700:
            n=Decimal(raw);precision=((mod-4)>>16)&65535;scale=(mod-4)&2047
            # Decimal context rounding must never change source numeric values.
            if not n.is_finite():raise ValueError()
            sign,digits,exponent=n.as_tuple()
            if exponent < -scale or max(len(digits)+exponent,0)>precision-scale:raise ValueError()
            return n
        if typ in (25,1043):return raw
    except (ValueError,InvalidOperation):pass
    raise CaptureError('unsupported_incremental_value')


def tuple_values(reader,columns):
    if reader.number('H')!=len(columns):raise CaptureError('schema_changed')
    result=[];size=0
    for column in columns:
        kind=reader.take(1)
        if kind==b'u':result.append(UNCHANGED)
        elif kind==b'n':result.append(None)
        elif kind==b't':
            raw=reader.take(reader.number('I'));size+=len(raw)
            try:result.append(value(raw.decode('utf8'),column))
            except UnicodeError:raise CaptureError('invalid_pgoutput') from None
        else:raise CaptureError('unsupported_tuple')
    if size>256*1024:raise CaptureError('row_budget')
    return result


def changes(payload,schema,end):
    result=[];begun=False;finished=False;final=None
    for frame in frames(payload):
        r=Reader(frame);tag=r.take(1)
        if tag==b'B' and not begun:
            final=r.number('Q');r.number('q');r.number('I');begun=True
        elif tag==b'C' and begun and not finished:
            if r.number('B')!=0 or r.number('Q')!=final or r.number('Q')!=end:raise CaptureError('spool_commit_mismatch')
            r.number('q');finished=True
        elif tag in (b'I',b'U',b'D') and begun and not finished:
            oid=str(r.number('I'))
            if oid not in schema:raise CaptureError('schema_changed')
            columns=schema[oid][3];pk=[i for i,c in enumerate(columns) if c[0]==1]
            if len(pk)!=1:raise CaptureError('integer_primary_key_required')
            pk=pk[0];marker=r.take(1);old=new=None
            if tag!=b'I' and marker in (b'K',b'O'):
                old=tuple_values(r,columns)
                if tag==b'U':marker=r.take(1)
            if tag!=b'D':
                if marker!=b'N':raise CaptureError('invalid_tuple')
                new=tuple_values(r,columns)
            elif old is None:raise CaptureError('missing_replica_identity')
            oldkey=(old or new)[pk];newkey=new[pk] if new else None
            if not isinstance(oldkey,int) or (new is not None and not isinstance(newkey,int)):raise CaptureError('invalid_primary_key')
            if tag==b'I' and any(v is UNCHANGED for v in new):raise CaptureError('invalid_unchanged_column')
            result.append((oid,tag,oldkey,newkey,new))
            if len(result)>MAX_ROWS:raise CaptureError('apply_row_budget')
        else:raise CaptureError('unsupported_pgoutput')
        r.finish()
    if not begun or not finished:raise CaptureError('incomplete_transaction')
    return result


def overlay(operations,existing,schema):
    # Existing contains only affected keys, read at the PREVIOUS published version.
    state={oid:dict(rows) for oid,rows in existing.items()}
    for oid,tag,oldkey,newkey,new in operations:
        rows=state[oid];previous=rows.get(oldkey)
        if tag==b'I':
            if rows.get(newkey) is not None:raise CaptureError('duplicate_source_key')
            rows[newkey]=new
        else:
            if previous is None:raise CaptureError('missing_source_key')
            if tag==b'D':rows[oldkey]=None
            else:
                resolved=[p if v is UNCHANGED else v for p,v in zip(previous,new)]
                if oldkey!=newkey:
                    if rows.get(newkey) is not None:raise CaptureError('duplicate_source_key')
                    rows[oldkey]=None
                rows[newkey]=resolved
    result={}
    for oid,rows in state.items():
        changed={key:row for key,row in rows.items() if row!=existing[oid].get(key)}
        if changed:result[oid]=changed
    return result
