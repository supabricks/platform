#!/usr/bin/env python3
"""SY02 real PG17 capture: frozen handoff, durability, lifecycle and fail-closed recovery."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import struct
import tempfile
import time
import psycopg
from cell import wait, lsn
from snapshot_policies import Policies


class Capture(Policies):
    def capture(self, kind, **fields):
        return self.api('managed_capture',command=dict(kind=kind,**fields))

    def status(self, cap):
        return self.capture('status',id=cap['id'])

    def state_is(self, cap, state):
        def done():
            c=self.status(cap)
            if c['state']=='resync_required' and state!='resync_required':raise AssertionError(c)
            return c if c['state']==state else False
        return wait(done,timeout=120)

    def begin_capture(self, policy, key, paused=False, wal=512*1024*1024):
        c=self.cli('sync','capture','start',policy['id'],'--revision',str(policy['revision']),
                   '--key',key,'--wal-bytes',str(wal))
        if paused:self.capture('pause',id=c['id'],key=key+'-pause')
        return c

    def delete_capture(self, cap):
        self.capture('delete',id=cap['id'],key='delete-'+cap['id'])
        self.state_is(cap,'deleted')
        assert not (self.root/'capture'/cap['id']).exists()
        assert self.sql(self.parent,"SELECT count(*) FROM pg_replication_slots WHERE slot_type='logical'")=='0'
        assert self.sql(self.parent,"SELECT count(*) FROM pg_publication WHERE pubname LIKE 'sbcap_%'")=='0'

    def journal(self, cap):
        path=self.root/'capture'/cap['id']/'spool/spool.sqlite3'
        with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as db:
            return [(int(end,16),payload) for end,payload in db.execute('SELECT end_lsn,payload FROM transactions ORDER BY seq')]

    def await_transactions(self, cap, count):
        return wait(lambda: (rows if len(rows:=self.journal(cap))>=count else False),timeout=30)

    def source(self):
        return psycopg.connect(host='127.0.0.1',port=self.parent['ports']['sql'],user='cloud_admin',
                               password=self.credentials(self.parent),dbname='postgres',autocommit=True)

    def run(self, python, worker):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "durable-capture"\n')
        self.start();self.request(method='resolve_binding',source=dict(definition_id=self.project,worktree=str(self.work)))
        self.parent=self.create('main');self.configure(worker)
        self.sql(self.parent,"CREATE TABLE orders(id int PRIMARY KEY,value text); CREATE TABLE payments(id int PRIMARY KEY); INSERT INTO orders VALUES(1,'first'); INSERT INTO payments VALUES(1)")
        p=self.sync('create',branch='main',key='policy',config=dict(mode='snapshot',strategy='full',schedule=None))
        cap=self.begin_capture(p,'first',paused=True)
        paused=self.state_is(cap,'paused');assert paused['start_lsn'] and paused['bootstrap_id'] is None,paused
        # Transaction is open across the frozen physical boundary. It must be absent
        # from the baseline and appear once, whole, in the durable E>F journal.
        with self.source() as long:
            long.execute('BEGIN');long.execute("INSERT INTO orders VALUES(2,'long'); INSERT INTO payments VALUES(2)")
            self.capture('resume',id=cap['id'],key='resume-first')
            ready=self.state_is(cap,'capturing')
            staging=self.root/'analytics/staging'/ready['bootstrap_id']
            manifest=json.loads((staging/'manifest.json').read_text())
            assert self.values(staging,manifest,'orders')==[dict(id=1,value='first')]
            assert self.values(staging,manifest,'payments')==[dict(id=1)]
            assert lsn(ready['start_lsn'])<=lsn(ready['bootstrap_lsn'])
            self.failed_api('publish_export',id=ready['bootstrap_id'])
            self.failed_api('discard_export',id=ready['bootstrap_id'])
            self.sql(self.parent,"BEGIN; INSERT INTO orders VALUES(99,'aborted'); ROLLBACK")
            long.execute('COMMIT')
        rows=self.await_transactions(cap,1)
        assert len(rows)==1 and rows[0][0]>lsn(ready['bootstrap_lsn'])
        assert change_tags(rows[0][1])==[b'I',b'I']
        self.check('isolated_frozen_baseline_long_transaction_handoff_and_private_publication_fence')
        slot='sbcap_'+cap['id'].replace('-','')
        with self.source() as db:
            confirmed=db.execute('SELECT confirmed_flush_lsn::text FROM pg_replication_slots WHERE slot_name=%s',(slot,)).fetchone()[0]
        assert lsn(confirmed)<=rows[-1][0]
        self.check('complete_multi_table_transaction_durable_before_source_ack_and_abort_absent')

        process=next(r for r in self.records() if r['role']=='capture-'+cap['id'])
        os.kill(process['pid'],signal.SIGKILL)
        self.sql(self.parent,"BEGIN; UPDATE orders SET id=20,value='updated' WHERE id=2; DELETE FROM payments WHERE id=2; COMMIT")
        rows=self.await_transactions(cap,2);assert len(rows)==2 and change_tags(rows[-1][1])==[b'U',b'D']
        self.state_is(cap,'capturing')
        self.stop();self.start();self.state_is(cap,'capturing')
        assert self.journal(cap)==rows
        self.check('worker_sigkill_and_daemon_compute_restart_replay_without_duplicate_or_gap')

        self.capture('pause',id=cap['id'],key='pause');self.state_is(cap,'paused')
        self.sql(self.parent,"INSERT INTO payments VALUES(3)")
        time.sleep(1.2);assert self.journal(cap)==rows
        self.capture('resume',id=cap['id'],key='resume');self.await_transactions(cap,3)
        self.check('pause_retains_owned_source_history_and_resume_captures_backlog')

        self.stop()
        with sqlite3.connect(self.root/'capture'/cap['id']/'spool/spool.sqlite3') as db:
            db.execute("UPDATE transactions SET payload=x'00' WHERE seq=1")
        self.start();bad=self.state_is(cap,'resync_required');assert bad['error'].startswith('spool_corrupt'),bad
        wait(lambda:self.status(cap)['cleanup_complete'])
        self.delete_capture(cap)
        self.check('corrupt_spool_requires_resync_and_cleanup_does_not_read_corrupt_journal')

        cap=self.begin_capture(p,'schema');self.state_is(cap,'capturing')
        self.sql(self.parent,'BEGIN; CREATE TABLE empty_probe(id int PRIMARY KEY); DROP TABLE empty_probe; COMMIT')
        bad=self.state_is(cap,'resync_required');assert bad['error'].startswith('schema_changed'),bad
        self.delete_capture(cap)
        self.check('empty_create_drop_ddl_transaction_fences_even_when_final_catalog_matches')

        cap=self.begin_capture(p,'history');self.state_is(cap,'capturing')
        self.capture('pause',id=cap['id'],key='pause-history');self.state_is(cap,'paused')
        slot='sbcap_'+cap['id'].replace('-','')
        with self.source() as db:db.execute('SELECT pg_drop_replication_slot(%s)',(slot,))
        bad=self.state_is(cap,'resync_required');assert bad['error'].startswith('source_history_lost'),bad
        self.delete_capture(cap)
        self.check('missing_source_slot_never_recreates_over_acknowledged_history')

        cap=self.begin_capture(p,'ack-ahead');self.state_is(cap,'capturing')
        self.capture('pause',id=cap['id'],key='pause-ack');self.state_is(cap,'paused')
        self.sql(self.parent,"INSERT INTO payments VALUES(4)")
        slot='sbcap_'+cap['id'].replace('-','')
        with self.source() as db:db.execute('SELECT pg_replication_slot_advance(%s,pg_current_wal_flush_lsn())',(slot,))
        bad=self.state_is(cap,'resync_required');assert bad['error'].startswith('source_ack_ahead_of_spool'),bad
        self.delete_capture(cap)
        self.check('source_acknowledgment_ahead_of_durable_spool_requires_resync')

        cap=self.begin_capture(p,'wal-pressure',paused=True,wal=32*1024*1024);self.state_is(cap,'paused')
        # Capturing is paused; the source monitor must still bound retention.
        self.sql(self.parent,"INSERT INTO orders SELECT i,md5(i::text) FROM generate_series(1000,300000) i")
        bad=self.state_is(cap,'resync_required');assert bad['error'].startswith(('wal_budget','source_history_lost')),bad
        self.delete_capture(cap)
        self.check('paused_wal_pressure_releases_owned_resources_and_requires_new_baseline')
        self.sql(self.parent,'CREATE SCHEMA pgapp; CREATE TABLE pgapp.outside(id int PRIMARY KEY)')
        cap=self.begin_capture(p,'non-system-schema')
        bad=self.state_is(cap,'resync_required');assert bad['error'].startswith('unsupported_source_relation'),bad
        self.delete_capture(cap)
        self.check('user_pg_prefix_schema_is_not_silently_classified_as_system_metadata')
        self.stop()


def change_tags(payload):
    # Independent framing check: no Relation or incomplete BEGIN/COMMIT in durable rows.
    offset=0;tags=[]
    while offset<len(payload):
        n=struct.unpack_from('!I',payload,offset)[0];offset+=4
        tags.append(payload[offset:offset+1]);offset+=n
    assert offset==len(payload) and tags[0]==b'B' and tags[-1]==b'C'
    return tags[1:-1]


def main():
    p=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-sy02-',dir='/tmp')).resolve();root.chmod(0o700)
    cell=Capture(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    report=dict(status='FAIL',checks=cell.checks)
    try:
        cell.run(args.python.absolute(),args.worker.resolve());report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:report.update(status='FAIL',cleanup='failed')
        if report['status']!='PASS':report['state_dir']=str(root)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS' and 'cleanup' not in report:shutil.rmtree(root)
    print(json.dumps(report,indent=2));assert report['status']=='PASS',report

if __name__=='__main__':main()
