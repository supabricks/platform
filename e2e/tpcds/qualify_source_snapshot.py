#!/usr/bin/env python3
"""Finish an EQ232 control's post-timing snapshot without replaying its load.

Keep the failed original receipt byte-for-byte. A new qualification directory
references the same stopped cell, with a receipt bound to the original hash.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'native'))
from installed_sync import InstalledContinuous
from cell import wait
from load import save


def run(args):
    source=args.load.resolve();original=(source/'result.json').read_bytes();loaded=json.loads(original)
    assert loaded['arm']=='source_only' and loaded['stopped']
    assert loaded['status']=='FAIL' and loaded['stage']=='post_timing_sync_bootstrap'
    assert loaded['committed_rows']==14770127 and loaded['load_seconds']>0
    assert loaded['release_identity']==hashlib.sha256((args.release/'release.json').read_bytes()).hexdigest()
    args.output.mkdir(exist_ok=False)
    (args.output/'state').symlink_to(source/'state',target_is_directory=True)
    cell=InstalledContinuous(args.release.resolve(),source/'state')
    with sqlite3.connect(f'file:{source}/state/state.sqlite3?mode=ro',uri=True) as db:
        policies=[json.loads(row[0]) for row in db.execute('SELECT record FROM sync_policies')]
        assert len(policies)==1;policy=policies[0]
    cell.project=policy['project_id'];cell.work=cell.root/'work';cell.policy_id=policy['id']
    cell.python=str(cell.release/'python/analytics/python')
    report=dict(loaded,status='RUNNING',stopped=False,stage='post_timing_snapshot_recovery',
        source_load_receipt_sha256=hashlib.sha256(original).hexdigest(),
        original_failure={k:loaded.get(k) for k in ('status','stage','error_type')},
        qualification_fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        qualification_scope='Post-timing source snapshot only; no repeated COPY or modified timing ledger')
    report.pop('error_type',None);started=time.monotonic()
    def checkpoint():
        report['post_timing_recovery_seconds']=time.monotonic()-started;save(args.output/'result.json',report)
    checkpoint()
    try:
        cell.start();cell.parent=cell.request(method='branch',id=policy['branch_id'])
        cell.parent=cell.state(cell.parent,'running');wait(lambda:cell.sql(cell.parent,'SELECT 1')=='1')
        # The stopped fixture owns an interrupted full bootstrap. Replace that
        # capture through the product's reviewed resync API; never replay COPY,
        # edit the control database or silently rewrite the old failure receipt.
        review=cell.cli('sync','review-resync',cell.policy_id)
        report['resync_review']=review;checkpoint()
        old_capture=review['capture_id']
        cell.cli('sync','resync',cell.policy_id,'--revision',str(review['expected_revision']),
                 '--review-hash',review['review_hash'],'--key','eq232-postload-resync')
        if old_capture:
            wait(lambda:cell.status(dict(id=old_capture))['state']=='deleted',timeout=120)
        current=cell.policy()
        cell.cli('sync','resume',cell.policy_id,'--revision',str(current['revision']),
                 '--key','eq232-postload-resume')
        while time.monotonic()-started < 900:
            current=cell.policy();capture=cell.status(dict(id=current['capture_id']))
            report['final_policy']=current;report['final_capture']=capture
            state=current['continuous_status']['state']
            print(json.dumps(dict(elapsed_seconds=time.monotonic()-started,state=state,capture=capture['state'])),flush=True)
            if state in ('blocked','failed'):
                raise RuntimeError('post-load snapshot requires explicit recovery; see retained status')
            if state=='healthy':
                report['publication']=cell.current()
                assert {t['name']:t['rows'] for t in report['publication']['descriptor']['manifest']['tables']}==loaded['source_rows']
                report.update(status='PREFIX_PASS',stage='loaded_requires_exact_verification_and_queries');break
            checkpoint();time.sleep(5)
        else:
            raise TimeoutError('post-timing snapshot qualification deadline')
    except BaseException as error:
        report.update(status='FAIL',error_type=type(error).__name__);raise
    finally:
        if (cell.root/'control.sock').exists():
            cell.stop();report['stopped']=True
        assert (source/'result.json').read_bytes()==original
        checkpoint()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('load','release','output'):p.add_argument('--'+name,type=Path,required=True)
    run(p.parse_args())
