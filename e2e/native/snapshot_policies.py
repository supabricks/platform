#!/usr/bin/env python3
"""SY01: real daemon-owned full refreshes, missed schedules, cancellation and pinned Sail readers."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid
from cell import wait
from sessions import Sessions


class Policies(Sessions):
    def sync(self, kind, **fields):
        return self.api('managed_snapshots', command=dict(kind=kind, **fields))

    def finished(self, run):
        r = wait(lambda: (r if (r := self.sync('run', id=run['id']))['state'] in
                          ('succeeded', 'failed', 'cancelled') else False), timeout=180)
        assert r['state'] == 'succeeded', r
        assert r['source_lsn'] and r['epoch_id'] and r['refresh_id'], r
        return r

    def check(self, name):
        self.checks.append(dict(name=name, status='PASS'))

    def run(self, python, worker):
        self.python = python
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / 'supabricks.toml').write_text(
            f'format_version = 1\nid = "{self.project}"\nname = "managed-snapshots"\n')
        self.start()
        self.request(method='resolve_binding', source=dict(definition_id=self.project, worktree=str(self.work)))
        parent = self.create('main')
        self.configure(worker)
        self.sql(parent, 'CREATE TABLE orders(id int PRIMARY KEY); CREATE TABLE payments(id int PRIMARY KEY); '
                         'INSERT INTO orders VALUES(1); INSERT INTO payments VALUES(1)')
        policy = self.cli('sync', 'create', '--branch', 'main', '--key', 'create')
        assert self.cli('sync', 'create', '--branch', 'main', '--key', 'create')['id'] == policy['id']
        assert policy['config']['mode'] == 'snapshot' and policy['config']['strategy'] == 'full'
        first = self.cli('sync', 'run', policy['id'], '--revision', '1', '--key', 'first')
        assert self.cli('sync', 'run', policy['id'], '--revision', '1', '--key', 'first')['id'] == first['id']
        first = self.finished(first)
        self.check('cli_and_api_durable_idempotent_full_refresh')
        reader = self.opened(ttl_ms=600000)
        assert reader['epoch_id'] == first['epoch_id']
        self.sql(parent, 'BEGIN; INSERT INTO orders VALUES(2); INSERT INTO payments VALUES(2); COMMIT')
        second = self.finished(self.sync('run_now', id=policy['id'], expected_revision=1, key='second'))
        current = self.api('current_snapshot', branch='main')['publication']
        assert current['epoch_id'] == second['epoch_id'] != first['epoch_id']
        assert len(self.rows(current, 'orders')) == len(self.rows(current, 'payments')) == 2
        assert self.query(reader, 'SELECT count(*) FROM public.orders')['rows'] == [['1']]
        self.close(reader)
        self.check('atomic_two_table_refresh_preserves_pinned_sail_reader')

        # A deliberately slow disposable exporter provides a deterministic cancellation window.
        slow = self.root / 'slow-export.py'
        slow.write_text('import time\ntime.sleep(60)\n')
        self.configure(slow)
        cancelled = self.sync('run_now', id=policy['id'], expected_revision=1, key='cancel')
        active = wait(lambda: (r if (r := self.sync('run', id=cancelled['id']))['refresh_id'] else False))
        self.sync('cancel', id=cancelled['id'], key='cancel-request')
        assert self.sync('run', id=cancelled['id'])['state'] == 'cancelled'
        self.terminal(self.api('get_export', id=active['refresh_id']), 'cancelled')
        assert self.api('current_snapshot', branch='main')['publication']['epoch_id'] == second['epoch_id']
        assert len(self.rows(current, 'payments')) == 2
        self.configure(worker)
        self.check('running_cancel_preserves_prior_epoch_and_cleans_owned_export')

        config = dict(mode='snapshot', strategy='full', schedule=dict(interval_seconds=60, timezone='UTC', missed_run='coalesce'))
        policy = self.sync('update', id=policy['id'], expected_revision=1, key='schedule', config=config)
        self.sql(parent, 'BEGIN; INSERT INTO orders VALUES(3); INSERT INTO payments VALUES(3); COMMIT')
        self.stop()
        # Advance the fixture clock across five missed periods while the daemon is stopped.
        # The unit suite separately injects exact times without touching wall-clock state.
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            record = json.loads(db.execute('SELECT record FROM sync_policies WHERE id=?', (policy['id'],)).fetchone()[0])
            record['next_due_at_ms'] = int(time.time()*1000) - 5*60*1000
            db.execute('UPDATE sync_policies SET record=? WHERE id=?', (json.dumps(record), policy['id']))
        self.start()
        due = wait(lambda: next((r for r in self.sync('runs', id=policy['id']) if r['trigger']=='schedule'), False))
        scheduled = self.finished(due)
        assert scheduled['epoch_id'] != second['epoch_id']
        assert len([r for r in self.sync('runs', id=policy['id']) if r['trigger']=='schedule']) == 1
        assert self.sync('get', id=policy['id'])['next_due_at_ms'] > int(time.time()*1000)
        latest = self.api('current_snapshot', branch='main')['publication']
        assert len(self.rows(latest, 'orders')) == len(self.rows(latest, 'payments')) == 3
        self.check('restart_coalesces_missed_intervals_without_browser_or_duplicate_runs')
        paused = self.cli('sync', 'pause', policy['id'], '--revision', '2', '--key', 'pause')
        assert paused['next_due_at_ms'] is None and paused['state'] == 'paused'
        self.stop(); self.start()
        assert self.sync('get', id=policy['id'])['state'] == 'paused'
        assert self.api('current_snapshot', branch='main')['publication']['epoch_id'] == scheduled['epoch_id']
        self.check('paused_policy_and_published_epoch_survive_restart')
        self.failed_api('managed_snapshots', command=dict(kind='update', id=policy['id'], expected_revision=3,
                        key='unsupported', config=dict(mode='continuous', strategy='incremental', schedule=None)))
        assert self.sync('get', id=policy['id'])['revision'] == 3
        self.check('unsupported_incremental_mode_rejected_without_mutation')
        self.stop()


def main():
    p = argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):
        p.add_argument('--'+name,type=Path,required=True)
    args = p.parse_args()
    root = Path(tempfile.mkdtemp(prefix='sb-sy01-',dir='/tmp')).resolve()
    root.chmod(0o700)
    cell = Policies(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    report = dict(status='FAIL', checks=cell.checks)
    try:
        cell.run(args.python.absolute(),args.worker.resolve())
        report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try: cell.stop()
            except Exception: report.update(status='FAIL', cleanup='failed')
        if report['status'] != 'PASS': report['state_dir']=str(root)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='PASS' and 'cleanup' not in report: shutil.rmtree(root)
    print(json.dumps(report,indent=2))
    assert report['status']=='PASS', report

if __name__=='__main__': main()
