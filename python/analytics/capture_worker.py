#!/usr/bin/env python3
"""Daemon-owned SY02 capture worker. Row contents stay in the private spool."""
import json
import os
from pathlib import Path
import select
import sqlite3
import sys
import time
import psycopg
from capture.spool import Spool, CaptureError, atomic, lsn, pg_lsn
from capture.protocol import Decoder, Wire
from capture.source import Source
from capture.bootstrap import verify


def read(path):
    if path.stat().st_size>65536:raise CaptureError('invalid_control')
    return json.loads(path.read_text())


def run(path):
    config=read(path);root=path.parent;identity=config['identity'];generation=config['worker_generation']
    os.umask(0o077)
    spool=source=wire=None
    last_report=0;last_source_check=0;last_ack=0;observed=None;baseline=None;verified=None;emitted=None;stream_observed=None;current=config
    def report(state,error=None):
        nonlocal last_report
        last_report=time.monotonic()
        progress=spool.progress(current.get('published_lsn')) if spool else None
        if progress is not None:progress['stream_observed_at_ms']=stream_observed
        atomic(root/'status.json',dict(identity=identity,worker_generation=generation,state=state,error=error,
            observed_at_ms=int(time.time()*1000),start_lsn=pg_lsn(spool.get('start')) if spool and spool.get('start') is not None else None,
            captured_lsn=pg_lsn(spool.captured) if spool and spool.captured is not None else None,
            source_lsn=observed['source'] if observed else None,retained_wal_bytes=observed['retained_bytes'] if observed else None,
            spool_bytes=spool.path.stat().st_size if spool else None,bootstrap_lsn=verified,barrier=spool.get('barrier') if spool else None,progress=progress))
    try:
        if config['desired']=='deleted':
            source=Source(config,None)
            source.cleanup();report('deleted');return
        spool=Spool(root/'spool',identity,config['spool_bytes'])
        source=Source(config,spool)
        profile=source.setup();report('established')
        while True:
            current=read(path)
            if current['identity']!=identity or current['worker_generation']!=generation:raise CaptureError('worker_fenced')
            if current['desired']=='deleted':
                if wire:wire.close();wire=None
                source.cleanup();report('deleted');return
            if current.get('bootstrap') and verified is None:
                if baseline is None:baseline=verify(current,spool)
                try:verified=next(baseline)
                except StopIteration:raise CaptureError('bootstrap_verification')
            if time.monotonic()-last_source_check>=1:
                observed=source.check();last_source_check=time.monotonic()
            if time.monotonic()-last_report>=max(.25,current.get('report_interval_ms',1000)/1000):
                report('paused' if current['desired']=='paused' else 'capturing')
            if current['desired']=='paused':
                if wire:wire.close();wire=None
                time.sleep(.1);continue
            requested=current.get('barrier_request')
            if verified and requested and requested!=emitted and (spool.get('barrier') or {}).get('run_id')!=requested:
                from capture.protocol import barrier_message
                prefix='supabricks.barrier.'+identity['generation']
                barrier_message(1,prefix,requested.encode(),prefix if identity.get('decoder_version')==2 else None)
                # A transactional message provides a complete commit boundary even
                # on an idle source. A restart may emit it again; the first durable
                # occurrence for the run wins, and a pinned target never moves.
                with source.conn.transaction():
                    source.conn.execute('SET LOCAL synchronous_commit=on')
                    source.conn.execute('SELECT pg_logical_emit_message(true,%s,%s)',(prefix,requested))
                emitted=requested
            if wire is None:
                observed=source.check()
                wire=Wire(config['socket_dir'],config['port'])
                wire.start(source.slot,source.publication,spool.captured)
                decoder=Decoder(profile['relations'],source.fence,'supabricks.barrier.'+identity['generation'] if identity.get('decoder_version')==2 else None)
            if not select.select([wire.socket],[],[],.2)[0]:
                if time.monotonic()-last_ack>=1:
                    wire.feedback(spool.captured,request=True);last_ack=time.monotonic()
                continue
            kind,end,data=wire.receive()
            stream_observed=int(time.time()*1000)
            if kind=='data':
                tx=decoder.feed(data)
                if tx:
                    spool.append(*tx)
                    # This value is re-read from FULL-synchronous SQLite, never from XLogData's WAL end.
                    wire.feedback(spool.captured);last_ack=time.monotonic()
            elif data:
                wire.feedback(spool.captured);last_ack=time.monotonic()
    except (CaptureError,sqlite3.Error,OSError,psycopg.Error,ValueError,KeyError,TypeError) as error:
        if wire:wire.close();wire=None
        code=error.code if isinstance(error,CaptureError) else 'spool_io' if isinstance(error,sqlite3.Error) else 'invalid_metadata' if isinstance(error,(ValueError,KeyError,TypeError)) else 'source_unavailable'
        # Resource/history/codec failures abandon this generation, never skip changes.
        # Source outages retain the slot under its server cap and can reconnect after restart.
        state='unavailable' if code=='source_unavailable' else 'resync_required'
        if state=='resync_required' and source:
            try:source.cleanup()
            except Exception:code+=':cleanup_pending'
        try:report(state,code)
        except OSError:pass  # No disk space is never an acknowledgment.
        return 1
    finally:
        if wire:wire.close()
        if source:source.close()
        if spool:spool.close()

if __name__=='__main__':
    sys.exit(run(Path(sys.argv[1])))
