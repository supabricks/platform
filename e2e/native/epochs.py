#!/usr/bin/env python3
"""A02: real Delta epochs, reference-safe collection and interrupted exports."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from cell import wait
from exports import Exports


class Epochs(Exports):
    def publication(self, export, expected='published'):
        def done():
            p=self.api('get_publication',id=export['id'])
            return p if p['state'] in ('published','failed','cancelled') else False
        p=wait(done,timeout=90)
        assert p['state']==expected,p
        return p

    def publish(self):
        export=self.terminal(self.begin())
        p=self.api('publish_export',id=export['id'])
        assert self.api('publish_export',id=export['id'])['epoch_id']==p['epoch_id']
        p=self.publication(export)
        return export,p

    def rows(self, publication, table):
        descriptor=publication['descriptor']
        generation=self.root/descriptor['generation']
        assert json.loads((generation/'snapshot.json').read_text())==descriptor
        assert not (self.root/'analytics/staging'/publication['export_id']).exists()
        return self.values(generation,descriptor['manifest'],table)

    def cli(self,*args):
        return json.loads(subprocess.check_output([str(self.binary),*args,'--project',str(self.work),
            '--data-dir',str(self.root),'--json'],text=True,timeout=20))

    def run(self, python, worker):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "epochs"\n')
        self.start();self.request(method='register_project',config=dict(format_version=1,id=self.project,name='epochs'))
        parent=self.create('main');self.configure(worker)
        self.sql(parent,"CREATE TABLE orders(id int,amount numeric(20,4)); CREATE TABLE payments(id int,amount numeric(20,4)); INSERT INTO orders VALUES(1,1234567890123456.1234); INSERT INTO payments SELECT * FROM orders")
        a,first=self.publish()
        expected=[dict(id=1,amount='1234567890123456.1234')]
        assert self.rows(first,'orders')==self.rows(first,'payments')==expected
        lease=self.cli('analytics','pin',first['epoch_id'],'--ttl-ms','300000')
        self.sql(parent,"BEGIN; INSERT INTO orders VALUES(2,2); INSERT INTO payments VALUES(2,2); COMMIT")
        b,second=self.publish()
        assert len(self.rows(second,'orders'))==len(self.rows(second,'payments'))==2
        assert self.rows(first,'orders')==expected
        assert self.cli('analytics','snapshot','--branch','main')['publication']['epoch_id']==second['epoch_id']
        assert len(self.cli('analytics','epochs','--branch','main')['snapshots'])==2
        page=self.cli('analytics','epochs','--branch','main','--limit','1')
        assert page['snapshots'][0]['publication']['descriptor'] is None
        older_page=self.cli('analytics','epochs','--branch','main','--before',str(page['next_before']))
        assert older_page['snapshots'][0]['publication']['epoch_id']==first['epoch_id']
        self.checks.append(dict(name='atomic_epochs_and_independent_delta_readers',status='PASS'))
        self.stop();self.start()
        self.cli('analytics','renew',lease['id'],'--ttl-ms','300000')
        assert self.rows(first,'payments')==expected
        assert self.api('collect_snapshots',branch='main',keep=1)['deleting']==[]
        # Analytical readers do not hold the source compute awake.
        parent=self.state(parent,'suspended')
        assert self.cli('analytics','snapshot','--branch','main')['publication']['epoch_id']==second['epoch_id']
        assert self.rows(first,'orders')==expected
        self.cli('analytics','unpin',lease['id'])
        assert self.cli('analytics','gc','--branch','main','--keep','1')['deleting']==[first['epoch_id']]
        wait(lambda:self.api('get_snapshot',id=first['epoch_id'])['state']=='deleted')
        assert not (self.root/first['descriptor']['generation']).exists()
        assert len(self.rows(second,'orders'))==2
        parent=self.state(parent,'running')
        self.checks.append(dict(name='restart_and_suspension_preserve_leased_history',status='PASS'))
        older=self.terminal(self.begin())
        self.sql(parent,"BEGIN; INSERT INTO orders VALUES(3,3); INSERT INTO payments VALUES(3,3); COMMIT")
        newer,current=self.publish()
        try:self.api('publish_export',id=older['id'])
        except RuntimeError:pass
        else:raise AssertionError('older export replaced a newer source snapshot')
        self.cli('analytics','discard',older['id'])
        wait(lambda:not (self.root/'analytics/staging'/older['id']).exists())
        self.checks.append(dict(name='stale_publication_and_unpublished_discard',status='PASS'))
        cancelled=self.terminal(self.begin());self.cli('analytics','publish',cancelled['id']);self.cli('analytics','discard',cancelled['id'])
        self.publication(cancelled,'cancelled')
        wait(lambda:not (self.root/'analytics/staging'/cancelled['id']).exists())
        assert self.api('current_snapshot',branch='main')['publication']['epoch_id']==current['epoch_id']
        self.checks.append(dict(name='cancel_publication_preserves_current',status='PASS'))
        # Kill the real worker after table/file writes and around its manifest.
        for boundary in ('table_1','table_2','before_manifest','after_manifest'):
            fault=self.root/'fault.py'
            fault.write_text(f'''import sys,os
from pathlib import Path
sys.path.insert(0,{str(worker.parent)!r})
import export
write=export.write_deltalake
atomic=export.atomic_json
count=0
def failed_write(*args,**kwargs):
 global count
 result=write(*args,**kwargs)
 count+=1
 if {boundary!r}==f'table_{{count}}':os._exit(71)
 return result
def failed_manifest(path,value):
 if Path(path).name=='manifest.json' and {boundary!r}=='before_manifest':os._exit(72)
 result=atomic(path,value)
 if Path(path).name=='manifest.json' and {boundary!r}=='after_manifest':os._exit(73)
 return result
export.write_deltalake=failed_write
export.atomic_json=failed_manifest
sys.exit(export.main())
''')
            self.configure(fault);failed=self.begin();self.terminal(failed,'failed')
            assert self.api('current_snapshot',branch='main')['publication']['epoch_id']==current['epoch_id']
            assert len(self.rows(current,'orders'))==len(self.rows(current,'payments'))==3
            self.checks.append(dict(name='interrupted_'+boundary,status='PASS'))
        self.configure(worker)
        corrupt=self.terminal(self.begin())
        manifest=Path(corrupt['outcome']['manifest']);data=json.loads(manifest.read_text());data['source']['lsn']='0/8';manifest.write_text(json.dumps(data))
        self.api('publish_export',id=corrupt['id']);self.publication(corrupt,'failed')
        wait(lambda:not manifest.parent.exists())
        assert self.api('current_snapshot',branch='main')['publication']['epoch_id']==current['epoch_id']
        self.checks.append(dict(name='invalid_manifest_preserves_previous_snapshot',status='PASS'))
        assert self.sql(parent,'SELECT count(*) FROM orders')=='3'
        self.stop()


def main():
    p=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-a02-',dir='/tmp')).resolve()
    cell=Epochs(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    report=dict(status='FAIL',checks=cell.checks,state_dir=str(root))
    try:cell.run(args.python.absolute(),args.worker.resolve());report['status']='PASS'
    finally:
        if (root/'control.sock').exists():
            try:cell.stop()
            except Exception:pass
        args.report.write_text(json.dumps(report,indent=2)+'\n')
        if (root/'daemon.log').exists():args.report.with_suffix('.log').write_bytes((root/'daemon.log').read_bytes())
        if report['status']=='PASS':shutil.rmtree(root)
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
