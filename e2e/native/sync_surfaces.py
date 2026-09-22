#!/usr/bin/env python3
"""SY06 service authority on real native snapshot workers; no shared user state."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid
from cell import wait
from snapshot_policies import Policies


class Surfaces(Policies):
    def run(self, python, worker):
        self.python = python
        self.work = self.root / 'work'
        self.work.mkdir()
        (self.work / 'supabricks.toml').write_text(
            f'format_version=1\nid="{self.project}"\nname="sync-surfaces"\n')
        self.start()
        self.request(method='resolve_binding', source=dict(definition_id=self.project, worktree=str(self.work)))
        parent = self.create('main'); branch = parent['branch']['id']
        self.configure(worker)
        self.sql(parent, 'CREATE TABLE orders(id int PRIMARY KEY); INSERT INTO orders VALUES(1)')
        with sqlite3.connect(self.root / 'state.sqlite3') as db:
            deployment = db.execute('SELECT id FROM deployments').fetchone()[0]
        identity = lambda **c: self.request(method='identity_admin', command=c)
        admin = lambda **c: self.request(method='authorization_admin', command=c)
        revision = lambda: admin(action='policy', deployment=deployment)['policy_revision']
        manager = identity(action='service', label='sync-manager')['principal_id']
        service = identity(action='service', label='sync-executor')['principal_id']
        other = identity(action='service', label='other-reader')['principal_id']
        token_for = lambda p: identity(action='issue_service', principal=p, scopes=['identity:self', 'project:control'], ttl_seconds=3600)['token']
        token = token_for(manager); other_token = token_for(other)
        for who in (manager, service, other):
            admin(action='set_role', deployment=deployment, subject=dict(kind='principal', id=who), role='viewer', expected_policy=revision(), key=str(uuid.uuid4()))
        def grant(who, cap, present=True):
            admin(action='set_data_grant', deployment=deployment, branch=branch, subject=dict(kind='principal', id=who), capability=cap, present=present, expected_policy=revision(), key=str(uuid.uuid4()))
        def governed(command, actor_token=None, principal=None, target=deployment):
            return self.request(method='authorized', envelope=dict(api_version=1, token=actor_token or token, channel='service', csrf=None,
                command=dict(action='workspace', command=dict(action='sync', deployment=target, request=dict(command=command, expected_policy=revision(), service_principal=principal)))))
        def denied(call):
            try: call()
            except RuntimeError: return
            raise AssertionError('unauthorized action succeeded')
        create = dict(kind='create', branch=branch, key='create', config=dict(mode='snapshot', strategy='full', schedule=None))
        denied(lambda: governed(create, principal=service))
        for cap in ('read', 'manage_sync', 'read_sync'): grant(manager, cap)
        denied(lambda: governed(create, principal=service))
        for cap in ('read', 'execute_sync'): grant(service, cap)
        inspection = governed(dict(kind='inspect', branch=branch))
        assert inspection['source_qualification'] == 'not_probed'
        assert self.sync('list') == []
        p = governed(create, principal=service)
        assert governed(create, principal=service)['id'] == p['id']
        assert governed(dict(kind='list'), actor_token=other_token) == []
        denied(lambda: governed(dict(kind='get', id=p['id']), actor_token=other_token))
        denied(lambda: governed(dict(kind='get', id=p['id']), target=str(uuid.uuid4())))
        self.check('read_only_inspection_separate_source_manage_executor_and_result_grants_no_leakage')
        first = governed(dict(kind='run_now', id=p['id'], expected_revision=p['revision'], key='first'))
        identity(action='revoke', principal=manager)
        denied(lambda: governed(dict(kind='get', id=p['id'])))
        first = self.finished(first)
        reader = self.opened(epoch=first['epoch_id'], ttl_ms=600000)
        assert self.query(reader, 'SELECT count(*) FROM public.orders')['rows'] == [['1']]
        self.check('native_background_snapshot_finishes_after_manager_session_revocation')
        token = token_for(manager)
        self.sql(parent, 'INSERT INTO orders VALUES(2)')
        second = self.finished(governed(dict(kind='run_now', id=p['id'], expected_revision=1, key='second')))
        assert second['epoch_id'] != first['epoch_id']
        assert self.query(reader, 'SELECT count(*) FROM public.orders')['rows'] == [['1']]
        self.close(reader)
        newer = self.opened(epoch=second['epoch_id'], ttl_ms=600000)
        assert self.query(newer, 'SELECT count(*) FROM public.orders')['rows'] == [['2']]
        self.close(newer)
        self.check('new_epochs_do_not_retarget_existing_sail_readers')
        identity(action='revoke', principal=service)
        denied(lambda: governed(dict(kind='run_now', id=p['id'], expected_revision=1, key='second')))
        denied(lambda: self.api('get_snapshot', id=first['epoch_id']))
        denied(lambda: self.opened(epoch=second['epoch_id'], ttl_ms=600000))
        assert governed(dict(kind='get', id=p['id']))['authority_status'] == 'revoked_or_unavailable'
        self.check('service_revocation_denies_replayed_admission_and_retained_epoch_new_readers')
        governed(dict(kind='delete', id=p['id'], expected_revision=governed(dict(kind='get',id=p['id']))['revision'], key='delete'))
        incremental = dict(create, key='incremental', config=dict(mode='continuous', strategy='incremental', schedule=None))
        denied(lambda: governed(incremental, principal=service))
        self.check('unsupported_governed_incremental_profile_stays_closed')
        # A new scoped service authority cannot bypass the frozen whole-branch profile.
        self.sql(parent, 'ALTER TABLE orders ENABLE ROW LEVEL SECURITY')
        p = governed(dict(create, key='rls'), principal=service)
        run = governed(dict(kind='run_now', id=p['id'], expected_revision=1, key='rls-run'))
        ended = wait(lambda: (r if (r := self.sync('run', id=run['id']))['state'] in ('failed', 'cancelled', 'succeeded') else False), timeout=180)
        assert ended['state'] != 'succeeded', ended
        governed(dict(kind='delete', id=p['id'], expected_revision=governed(dict(kind='get',id=p['id']))['revision'], key='delete-rls'))
        self.check('native_rls_source_never_publishes_through_service_authority')
        audit = identity(action='audit_export', after=0)
        assert any(e['event'].get('stream') == 'sync' for e in audit['events'])
        self.check('admission_and_run_transition_audit_contains_identifiers_without_rows_or_tokens')
        self.stop()


def main():
    parser = argparse.ArgumentParser()
    for name in ('binary', 'bundle', 'helpers', 'python', 'worker', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='sb-sy06-')); root.chmod(0o700)
    cell = Surfaces(args.binary.resolve(), args.bundle.resolve(), args.helpers.resolve(), root)
    report = dict(status='FAIL', checks=cell.checks)
    try:
        cell.run(args.python.absolute(), args.worker.resolve()); report['status'] = 'PASS'
    finally:
        if (root / 'control.sock').exists():
            try: cell.stop()
            except Exception: report.update(status='FAIL', cleanup='failed')
        if report['status'] != 'PASS': report['state_dir'] = str(root)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        if report['status'] == 'PASS' and 'cleanup' not in report: shutil.rmtree(root)
    print(json.dumps(report, indent=2)); assert report['status'] == 'PASS'

if __name__ == '__main__': main()
