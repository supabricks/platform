"""Installed CLI -> continuous policy -> worker -> publication -> Sail profile binding."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from inputs import sha


def run(release,output):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
    from installed_sync import InstalledContinuous
    from cell import wait
    os.umask(0o077)
    output.mkdir(parents=True,exist_ok=False)
    root=output/'state';root.mkdir()
    cell=InstalledContinuous(release,root)
    report=dict(status='FAIL',scope=__doc__,checks=cell.checks,
        fixture_sha256=sha(Path(__file__)),release_identity=sha(release/'release.json'))
    started=time.monotonic();running=False
    try:
        cell.setup_source(str(release/'python/analytics/python'),release/'python/analytics/export.py',
            'CREATE TABLE orders(id integer PRIMARY KEY,value integer); INSERT INTO orders VALUES(1,10)')
        running=True
        policy=cell.cli('sync','create','--branch','main','--mode','continuous','--storage-profile','large','--key','large')
        cell.policy_id=policy['id']
        assert policy['config']['storage_profile']=='large'
        cell.healthy();first=cell.current()
        assert first['descriptor']['manifest']['storage_profile']=='large'
        reader=cell.opened(epoch=first['epoch_id'],ttl_ms=600000)
        cell.sql(cell.parent,'INSERT INTO orders VALUES(2,20),(3,30)')
        def published():
            cell.healthy();current=cell.current()
            return current if current['descriptor']['manifest']['tables'][0]['rows']==3 else False
        latest=wait(published,timeout=90)
        assert latest['descriptor']['manifest']['storage_profile']=='large'
        assert sorted(cell.version_rows(latest,'orders'),key=lambda row:row['id'])==[
            {'id':1,'value':10},{'id':2,'value':20},{'id':3,'value':30}]
        assert cell.query(reader,'SELECT count(*) FROM public.orders')['rows']==[['1']]
        current=cell.opened(epoch=latest['epoch_id'],ttl_ms=600000)
        assert cell.query(current,'SELECT sum(value) FROM public.orders')['rows']==[['60']]
        cell.close(current);cell.close(reader)
        cell.check('large_profile_cli_capture_publication_exact_delta_and_pinned_sail')
        p=cell.policy();config=dict(p['config'],storage_profile='compact')
        try:cell.sync('update',id=p['id'],expected_revision=p['revision'],key='forbidden-resize',config=config)
        except RuntimeError as error:assert 'storage profile' in str(error)
        else:raise AssertionError('live capture accepted profile downgrade')
        cell.check('live_capture_profile_change_rejected')
        cell.stop();running=False
        cell.start();running=True;cell.healthy()
        assert cell.policy()['config']['storage_profile']=='large'
        assert cell.current()['descriptor']['manifest']['storage_profile']=='large'
        historical=cell.opened(epoch=first['epoch_id'],ttl_ms=600000)
        assert cell.query(historical,'SELECT sum(value) FROM public.orders')['rows']==[['10']]
        cell.close(historical)
        cell.check('profile_and_historical_reader_survive_daemon_restart')
        report['epochs']=[first['epoch_id'],latest['epoch_id']]
        cell.stop();running=False
        report['status']='PASS'
    except BaseException as error:
        report['error']=str(error)
        raise
    finally:
        if running:
            try:cell.stop()
            except Exception as error:report['cleanup_error']=str(error)
        report['elapsed_seconds']=time.monotonic()-started
        (output/'result.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();run(args.release.resolve(),args.output.resolve())
