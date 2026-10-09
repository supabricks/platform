#!/usr/bin/env python3
"""EQ232: independently compare the isolated PG control with generated TPC-DS.

Bind every frozen COPY payload to the acknowledged ledger, then compare typed,
order-independent row digests for all 24 tables. No snapshot limit is raised.
"""
import argparse
from collections import defaultdict
import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'native'))
from inputs import LOCK, inventory, sha
from load import batches, save
from verify import digest_rows


def typed(raw, column):
    if not raw:
        assert column['nullable']; return None
    kind=column['type']; text=raw.decode('latin1')
    if kind=='integer':return int(text)
    if kind=='date':return datetime.date.fromisoformat(text)
    if kind.startswith('decimal('):
        scale=int(kind[8:-1].split(',')[1]);value=Decimal(text).quantize(Decimal(1).scaleb(-scale))
        return abs(value) if value==0 else value
    if kind.startswith('char('):
        width=int(kind[5:-1]);assert len(text)<=width;return text.ljust(width)
    assert kind=='text' or kind.startswith('varchar('), kind
    return text


def expected_rows(path, columns, count):
    if not count:return
    with path.open('rb') as stream:
        for index in range(count):
            raw=stream.readline();assert raw.endswith(b'|\n'), 'incomplete generated row'
            fields=raw[:-2].split(b'|');assert len(fields)==len(columns)
            yield [typed(v,c) for v,c in zip(fields,columns)]


def bind_copy_inputs(table, path, attempts):
    if not attempts:return
    frozen=batches(path, table['columns'])
    for record in attempts:
        begin,end,count,data=next(frozen)
        assert (begin,end,count,len(data),hashlib.sha256(data).hexdigest()) == (
            record['start_offset'],record['end_offset'],record['rows'],record['encoded_bytes'],record['copy_sha256'])


def run(args):
    from installed_sync import InstalledContinuous
    from cell import wait
    source=args.load.resolve();original=(source/'result.json').read_bytes();loaded=json.loads(original)
    assert loaded['arm']=='source_only' and loaded['stopped']
    assert loaded['status']=='SOURCE_LOAD_PASS' or (loaded['status']=='FAIL' and loaded['stage']=='post_timing_sync_bootstrap')
    assert loaded['committed_rows']==14770127 and loaded['load_seconds']>0
    assert loaded['release_identity']==sha(args.release/'release.json')
    assert loaded['input_lock_sha256']==sha(LOCK)
    assert loaded['generation_receipt_sha256']==sha(args.dataset/'generation.json')
    manifest=inventory(json.loads(LOCK.read_text()),args.inputs);assert len(manifest['tables'])==24
    attempts=defaultdict(list)
    ledger=[json.loads(s) for s in (source/'commits.jsonl').read_text().splitlines()]
    for index in range(0,len(ledger),2):
        attempt,ack=ledger[index:index+2]
        assert attempt['kind']=='attempt' and ack['kind']=='ack'
        assert all(attempt[k]==ack[k] for k in ('table','end_offset','rows'))
        attempts[attempt['table']].append(attempt)
    assert sum(r['rows'] for records in attempts.values() for r in records)==loaded['committed_rows']
    args.output.mkdir(exist_ok=False)
    cell=InstalledContinuous(args.release.resolve(),source/'state')
    with sqlite3.connect(f'file:{source}/state/state.sqlite3?mode=ro',uri=True) as db:
        policies=[json.loads(row[0]) for row in db.execute('SELECT record FROM sync_policies')]
    policy=policies[0] if policies else None
    cell.project=loaded.get('project_id') or policy['project_id'];cell.work=cell.root/'work'
    cell.python=str(cell.release/'python/analytics/python')
    report=dict(status='RUNNING',stopped=False,rows=loaded['committed_rows'],release_identity=loaded['release_identity'],
        source_load_receipt_sha256=hashlib.sha256(original).hexdigest(),
        original_load_status=loaded['status'],fixture_sha256=sha(Path(__file__)),tables=[],
        scope='Exact generated-prefix versus PostgreSQL control; no Delta bootstrap qualification',
        comparison='Typed order-independent partition hashes preserve duplicates; every COPY payload also matches the original ledger')
    started=time.monotonic()
    def checkpoint():
        report['elapsed_seconds']=time.monotonic()-started;save(args.output/'result.json',report)
    checkpoint()
    try:
        cell.start();branch_id=loaded.get('branch_id') or policy['branch_id']
        cell.parent=cell.request(method='branch',id=branch_id);cell.parent=cell.state(cell.parent,'running')
        wait(lambda:cell.sql(cell.parent,'SELECT 1')=='1')
        # Retire only the failed post-timing enrollment, if the old harness made
        # one. Its original failure receipt remains untouched; source data stays.
        if policy:
            cell.policy_id=policy['id'];current=cell.policy();capture=current.get('capture_id')
            report['retired_post_timing_policy']=cell.cli('sync','delete',cell.policy_id,'--revision',str(current['revision']),'--key','eq232-source-oracle-retire')
            if capture:wait(lambda:cell.status(dict(id=capture))['state']=='deleted',timeout=120)
        import psycopg
        with cell.source() as db:
            db.execute("SET DateStyle='ISO,YMD'");db.execute("SET statement_timeout='600s'")
            with db.transaction():
                db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
                for table in manifest['tables']:
                    name=table['name'];path=args.dataset/'data'/(name+'.dat');before=path.stat()
                    count=loaded['source_rows'][name];assert count==sum(a['rows'] for a in attempts[name])
                    bind_copy_inputs(table,path,attempts[name]);dest=args.output/name;dest.mkdir()
                    expected=digest_rows(expected_rows(path,table['columns'],count),dest/'expected')
                    with db.cursor(name='eq232_source_oracle') as cursor:
                        cursor.itersize=8192
                        cursor.execute(psycopg.sql.SQL('SELECT {} FROM {}').format(
                            psycopg.sql.SQL(',').join(psycopg.sql.Identifier(c['name']) for c in table['columns']),psycopg.sql.Identifier(name)))
                        actual=digest_rows(cursor,dest/'source')
                    after=path.stat();assert (before.st_size,before.st_mtime_ns)==(after.st_size,after.st_mtime_ns)
                    result=dict(table=name,rows=count,status='PASS' if expected==actual else 'MISMATCH',expected=expected,source=actual,
                                copy_transactions=len(attempts[name]))
                    save(dest/'result.json',result);report['tables'].append(dict(table=name,rows=count,status=result['status'],report_sha256=sha(dest/'result.json')))
                    print(json.dumps(report['tables'][-1]),flush=True);checkpoint()
                    assert expected==actual,name
        report['status']='SOURCE_EXACT_PASS'
    except BaseException as error:
        report.update(status='FAIL',error_type=type(error).__name__);raise
    finally:
        if (cell.root/'control.sock').exists():cell.stop();report['stopped']=True
        assert (source/'result.json').read_bytes()==original;checkpoint()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('load','release','inputs','dataset','output'):p.add_argument('--'+name,type=Path,required=True)
    run(p.parse_args())
