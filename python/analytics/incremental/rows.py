"""Strict pgoutput row conversion and transaction-ordered primary-key overlay."""
from decimal import Decimal, InvalidOperation
from datetime import date
from functools import lru_cache
import struct
from capture.spool import CaptureError, frames, MAX_MESSAGE
from capture.protocol import Reader,barrier_message

UNCHANGED = object()
MAX_ROWS = 16384
MAX_VALUES = 32*1024*1024
_INT_LIMITS={typ:(-(1<<(bits-1)),1<<(bits-1)) for typ,bits in ((20,64),(21,16),(23,32))}
_LENGTH=struct.Struct('!I')


@lru_cache(maxsize=128)
def numeric_shape(mod):
    precision=((mod-4)>>16)&65535;scale=(mod-4)&2047
    return precision-scale,scale,Decimal((0,(1,),-scale))


def key_columns(columns):
    keys=[i for i,c in enumerate(columns) if c[0]==1]
    if not keys or any(columns[i][2] not in (20,21,23) for i in keys):
        raise CaptureError('integer_primary_key_required')
    return keys


def row_key(row,keys):
    values=tuple(row[i] for i in keys)
    if any(type(v) is not int for v in values):raise CaptureError('invalid_primary_key')
    # Retain scalar single-key plans so in-flight pre-upgrade plans still replay.
    return values[0] if len(values)==1 else values


def key_values(key,keys):
    values=(key,) if len(keys)==1 else key
    if not isinstance(values,(list,tuple)) or len(values)!=len(keys) or any(type(v) is not int for v in values):
        raise CaptureError('invalid_primary_key')
    return values


def value(raw, column):
    if raw is None:return None
    typ=column[2];mod=column[3]
    try:
        if typ in _INT_LIMITS:
            n=int(raw);low,high=_INT_LIMITS[typ]
            if not low<=n<high:raise ValueError()
            return n
        if typ==1700:
            n=Decimal(raw);integer_digits,scale,quantum=numeric_shape(mod)
            # Decimal context rounding must never change source numeric values.
            if not n.is_finite():raise ValueError()
            # PostgreSQL typmod text normally has exactly the declared scale.
            # These exact Decimal predicates avoid allocating a digits tuple for
            # that common case; neither rounds nor consults context precision.
            if n.same_quantum(quantum):
                if integer_digits<0 or n.adjusted()>=integer_digits:raise ValueError()
                return n
            sign,digits,exponent=n.as_tuple()
            if exponent < -scale or max(len(digits)+exponent,0)>integer_digits:raise ValueError()
            return n
        if typ in (25,1043):return raw
        if typ==1042:
            if not 5<=mod<=10_485_764 or len(raw)!=mod-4:raise ValueError()
            return raw
        if typ==1082:
            if len(raw)!=10 or raw[4]!='-' or raw[7]!='-':raise ValueError()
            # Match bootstrap's finite Python/Arrow DATE profile (years 1–9999).
            # Wire startup pins ISO/YMD independently of database DateStyle.
            return date.fromisoformat(raw)
    except (ValueError,InvalidOperation):pass
    raise CaptureError('unsupported_incremental_value')


def tuple_values(reader,columns,*,allow_unchanged=True):
    if reader.number('H')!=len(columns):raise CaptureError('schema_changed')
    result=[];size=0;data=reader.data;position=reader.offset;limit=len(data)
    for column in columns:
        if position>=limit:raise CaptureError('invalid_pgoutput')
        kind=data[position];position+=1
        if kind==117:  # u: unchanged TOAST
            if not allow_unchanged:raise CaptureError('invalid_unchanged_column')
            result.append(UNCHANGED)
        elif kind==110:result.append(None)  # n: NULL
        elif kind==116:  # t: length-prefixed UTF-8 text
            if position+4>limit:raise CaptureError('invalid_pgoutput')
            length=_LENGTH.unpack_from(data,position)[0];position+=4
            end=position+length
            if end>limit:raise CaptureError('invalid_pgoutput')
            raw=data[position:end];position=end;size+=length
            try:result.append(value(raw.decode('utf8'),column))
            except UnicodeError:raise CaptureError('invalid_pgoutput') from None
        else:raise CaptureError('unsupported_tuple')
    if size>256*1024:raise CaptureError('row_budget')
    reader.offset=position
    return result


def changes(payload,schema,end,barrier_prefix=None):
    result=[];begun=False;finished=False;final=None;profiles={}
    for frame in frames(payload):
        r=Reader(frame);tag=r.take(1)
        if tag==b'B' and not begun:
            final=r.number('Q');r.number('q');r.number('I');begun=True
        elif tag==b'C' and begun and not finished:
            if r.number('B')!=0 or r.number('Q')!=final or r.number('Q')!=end:raise CaptureError('spool_commit_mismatch')
            r.number('q');finished=True
        elif tag==b'M' and begun and not finished:
            flags=r.number('B');r.number('Q');prefix=r.string();content=r.take(r.number('I'))
            barrier_message(flags,prefix,content,barrier_prefix)
        elif tag in (b'I',b'U',b'D') and begun and not finished:
            oid=str(r.number('I'))
            if oid not in schema:raise CaptureError('schema_changed')
            if oid not in profiles:
                columns=schema[oid][3];profiles[oid]=(columns,key_columns(columns))
            columns,pk=profiles[oid]
            marker=r.take(1);old=new=None
            if tag!=b'I' and marker in (b'K',b'O'):
                old=tuple_values(r,columns)
                if tag==b'U':marker=r.take(1)
            if tag!=b'D':
                if marker!=b'N':raise CaptureError('invalid_tuple')
                new=tuple_values(r,columns,allow_unchanged=tag!=b'I')
            elif old is None:raise CaptureError('missing_replica_identity')
            oldkey=row_key(old if old is not None else new,pk)
            # Without an old-key tuple both keys come from the same already
            # validated new row. Key-changing updates still validate both.
            newkey=(oldkey if old is None else row_key(new,pk)) if new is not None else None
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
