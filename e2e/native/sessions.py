#!/usr/bin/env python3
"""A03 native Sail session/user-surface and ownership qualification."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

from cell import wait
from epochs import Epochs


class Sessions(Epochs):
    def ready(self, s, expected='ready'):
        result=wait(lambda:(r if (r:=self.api('analytics_session',id=s['id']))['state'] in (('ready','failed','closed') if expected=='ready' else ('failed','closed')) else False),timeout=210)
        assert result['state']==expected,result
        return result

    def opened(self, **fields):
        return self.ready(self.api('analytics_open',branch='main',key=str(uuid.uuid4()),**fields))

    def query(self,s,sql,expected='complete',**limits):
        q=self.api('analytics_sql',id=s['id'],sql=sql,**limits)
        r=wait(lambda:(r if (r:=self.api('analytics_query',id=s['id'],query=q['id']))['state']!='running' else False),timeout=45)
        assert r['state']==expected,r
        assert r['epoch_id']==s['epoch_id'],r
        return r

    def close(self,s,verb='analytics_close'):
        self.api(verb,id=s['id'])
        return self.ready(s,'closed')

    def refresh(self):
        op=self.api('analytics_refresh',branch='main',key=str(uuid.uuid4()))
        r=wait(lambda:(r if (r:=self.api('analytics_status',id=op['id']))['state'] in ('published','failed','cancelled') else False),timeout=180)
        assert r['state']=='published',r
        return r['publication']

    def raw(self,s,expected):
        code='''import json,sys
from pyspark.sql import SparkSession
spark=SparkSession.builder.remote(sys.argv[1]).getOrCreate()
assert spark.table('public.orders').count()==int(sys.argv[2])
assert spark.sql('SELECT count(*) n FROM public.orders o JOIN public.payments p ON o.id=p.id').first().n==int(sys.argv[2])
assert spark.sql('SELECT epoch_id FROM _supabricks.epoch').first().epoch_id==sys.argv[3]
assert spark.conf.get('supabricks.epoch_id')==sys.argv[3]
print('PASS')
'''
        script=self.root/'raw-client.py';script.write_text(code)
        assert subprocess.check_output([str(self.python),str(script),s['endpoint'],str(expected),s['epoch_id']],text=True,timeout=45).strip()=='PASS'

    def failed_api(self, action, **fields):
        try:self.api(action,**fields)
        except RuntimeError:return
        raise AssertionError(f'{action} unexpectedly succeeded')

    def mcp(self, name, **fields):
        frames=[dict(jsonrpc='2.0',id=1,method='initialize',params=dict(protocolVersion='2025-06-18',clientInfo=dict(name='a03-native',version='1'),capabilities={})),
                dict(jsonrpc='2.0',method='notifications/initialized'),
                dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name=name,arguments=fields))]
        result=subprocess.run([str(self.binary),'mcp','--project',str(self.work),'--data-dir',str(self.root)],
            input=''.join(json.dumps(f)+'\n' for f in frames),capture_output=True,text=True,timeout=30,check=True)
        reply=json.loads(result.stdout.splitlines()[-1])['result']
        assert not reply['isError'],reply
        return reply['structuredContent']

    def run(self,python,worker):
        self.python=python;self.work=self.root/'work';self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "sessions"\n')
        self.start();self.request(method='register_project',config=dict(format_version=1,id=self.project,name='sessions'))
        parent=self.create('main');self.configure(worker)
        self.sql(parent,"CREATE TABLE orders(id int,amount numeric(20,4)); CREATE TABLE payments(id int,amount numeric(20,4)); INSERT INTO orders VALUES(1,1234567890123456.1234); INSERT INTO payments SELECT * FROM orders")
        self.sql(parent,"CREATE TABLE empty(id int); CREATE TABLE types(b bool,n bigint,t text,d date,ts timestamp,tz timestamptz); INSERT INTO types VALUES(true,9223372036854775807,'héllo','2024-02-29','2024-02-29 12:34:56.123456','2024-02-29 12:34:56.123456+05'); INSERT INTO types DEFAULT VALUES")
        # No manual A01/A02 calls: first access captures, exports and publishes.
        first=self.opened(ttl_ms=600000)
        assert self.query(first,'SELECT sum(o.amount) total FROM public.orders o JOIN public.payments p ON o.id=p.id')['rows']==[['1234567890123456.1234']]
        assert self.query(first,'SELECT amount FROM public.orders WHERE amount = 1234567890123456.1234')['rows']==[['1234567890123456.1234']]
        assert self.query(first,'SELECT * FROM public.empty')['rows']==[]
        types=self.query(first,'SELECT * FROM public.types ORDER BY n NULLS LAST')['rows']
        assert types==[['True','9223372036854775807','héllo','2024-02-29','2024-02-29 12:34:56.123456','2024-02-29 07:34:56.123456'],[None]*6],types
        self.raw(first,1)
        self.checks.append(dict(name='first_access_sql_dataframe_and_epoch_discovery',status='PASS'))
        slow=self.root/'slow-client.py';marker=self.root/'query-entered'
        slow.write_text(f"from pathlib import Path\nimport time\nfrom pyspark.sql import SparkSession\nspark=SparkSession.builder.remote({first['endpoint']!r}).getOrCreate()\ndef hold(value):\n Path({str(marker)!r}).write_text('executing')\n time.sleep(12)\n return value\nspark.udf.register('hold_epoch',hold,'int')\nassert [r.id for r in spark.sql('SELECT hold_epoch(id) AS id FROM public.orders').collect()]==[1]\nprint('PINNED_QUERY_PASS')\n")
        log=(self.root/'slow-client.log').open('w')
        reader=subprocess.Popen([str(self.python),str(slow)],stdout=log,stderr=subprocess.STDOUT)
        try:
            def executing():
                if reader.poll() is not None:raise AssertionError((self.root/'slow-client.log').read_text())
                return marker.exists()
            wait(executing,timeout=40)
            self.sql(parent,'BEGIN; INSERT INTO orders VALUES(2,2); INSERT INTO payments VALUES(2,2); COMMIT')
            new=self.refresh()
            assert reader.wait(timeout=45)==0,(self.root/'slow-client.log').read_text()
        finally:
            if reader.poll() is None:reader.kill();reader.wait(timeout=10)
            log.close()
        second=self.opened(ttl_ms=600000)
        self.checks.append(dict(name='refresh_during_running_dataframe_query_preserves_epoch',status='PASS'))
        assert first['epoch_id']!=second['epoch_id']==new['epoch_id']
        self.raw(first,1);self.raw(second,2)
        assert len(self.rows(new,'orders'))==2
        assert self.api('collect_snapshots',branch='main',keep=1)['deleting']==[]
        self.failed_api('analytics_open',branch='main',key=str(uuid.uuid4()))
        self.checks.append(dict(name='independent_epochs_raw_clients_retention_and_admission',status='PASS'))
        pageserver=next(p for p in self.records() if p['role']=='pageserver')
        os.kill(pageserver['pid'],signal.SIGKILL)
        assert self.query(first,'SELECT count(*) FROM public.orders')['rows']==[['1']]
        wait(lambda:self.request(method='status')['runtime']['ready'],timeout=60)
        assert self.api('analytics_session',id=first['id'])['endpoint']==first['endpoint']
        self.checks.append(dict(name='storage_recovery_preserves_analytical_worker',status='PASS'))
        for sql in ('DELETE FROM public.orders','SELECT 1; DROP VIEW public.orders',"WITH x AS (SELECT 1) INSERT INTO public.orders SELECT * FROM x"):
            self.query(second,sql,expected='failed')
        assert self.query(second,"SELECT 'delete; -- literal' AS text")['rows']==[['delete; -- literal']]
        self.query(second,'SELECT * FROM public.missing',expected='failed')
        limited=self.query(second,'SELECT * FROM range(10000)',max_rows=3)
        assert len(limited['rows'])==3 and limited['truncated']
        limited=self.query(second,"SELECT repeat('x',2000) FROM range(1000)",max_bytes=4096)
        assert len(json.dumps(limited).encode())<=4096 and limited['truncated']
        self.checks.append(dict(name='read_only_sql_row_byte_limits_and_query_errors',status='PASS'))
        parent=self.state(parent,'suspended')
        assert self.query(first,'SELECT count(*) FROM public.orders')['rows']==[['1']]
        assert self.query(second,'SELECT count(*) FROM public.orders')['rows']==[['2']]
        self.checks.append(dict(name='queries_do_not_wake_postgres',status='PASS'))
        self.close(first)
        old=first['epoch_id']
        assert self.api('collect_snapshots',branch='main',keep=1)['deleting']==[old]
        wait(lambda:self.api('get_snapshot',id=old)['state']=='deleted')
        self.close(second)
        # The ordinary CLI opens/closes an ephemeral session; the shell has a
        # standard SparkSession and ordinary DataFrame rows.
        command=[str(self.binary),'analytics','sql','--branch','main','--sql','SELECT count(*) FROM public.orders','--project',str(self.work),'--data-dir',str(self.root)]
        value=json.loads(subprocess.check_output(command,text=True,timeout=60))
        assert value['rows']==[['2']]
        wait(lambda:not self.session_records())
        script=self.root/'client.py';script.write_text("assert spark.table('public.orders').count()==2\nassert epoch['epoch_id']\nprint('SHELL_PASS')\n")
        command=[str(self.binary),'spark','shell','--branch','main','--file',str(script),'--project',str(self.work),'--data-dir',str(self.root)]
        assert 'SHELL_PASS' in subprocess.check_output(command,text=True,timeout=60)
        wait(lambda:not self.session_records())
        self.checks.append(dict(name='cli_sql_and_spark_shell_cleanup',status='PASS'))
        s=self.opened(ttl_ms=600000)
        # A running query is cancelled by stopping its entire session. A fresh
        # session is needed afterward; there is no silent replacement endpoint.
        self.api('analytics_sql',id=s['id'],sql='SELECT sum(a.id*b.id) FROM range(100000000) a CROSS JOIN range(100000000) b',timeout_ms=30000)
        self.close(s,'analytics_cancel');assert not self.session_records()
        s=self.opened(ttl_ms=600000)
        self.query(s,'SELECT sum(a.id*b.id) FROM range(100000000) a CROSS JOIN range(100000000) b',expected='failed',timeout_ms=100)
        self.ready(s,'failed');assert not self.session_records()
        self.checks.append(dict(name='running_query_cancellation_and_deadline_release_references',status='PASS'))
        s=self.opened(ttl_ms=600000)
        record=next(p for p in self.session_records() if p['role']=='analytics-session-'+s['id'])
        os.kill(record['pid'],signal.SIGKILL)
        self.ready(s,'failed');assert not self.session_records()
        # Storage and other branches survive analytical worker failure.
        assert self.request(method='status')['runtime']['ready']
        self.checks.append(dict(name='worker_crash_cleanup_independent_of_storage',status='PASS'))
        s=self.opened(ttl_ms=600000)
        os.kill(self.daemons[-1].pid,signal.SIGKILL);self.daemons[-1].wait(timeout=10)
        self.start();self.ready(s,'failed');assert not self.session_records()
        assert self.api('current_snapshot',branch='main')['publication']['epoch_id']==new['epoch_id']
        s=self.opened(ttl_ms=10000);self.ready_after_expiry(s)
        self.checks.append(dict(name='daemon_crash_recovery_and_session_expiration',status='PASS'))
        s=self.ready(self.mcp('analytics_open',epoch=new['epoch_id'],key=str(uuid.uuid4())))
        assert self.mcp('analytics_session',id=s['id'])['snapshot_age_ms']>=0
        q=self.mcp('analytics_sql',id=s['id'],sql='SELECT count(*) FROM public.orders')
        r=wait(lambda:(r if (r:=self.mcp('analytics_query',id=s['id'],query=q['id']))['state']!='running' else False),timeout=30)
        assert r['rows']==[['2']],r
        self.mcp('analytics_close',id=s['id']);self.ready(s,'closed')
        self.checks.append(dict(name='mcp_session_query_and_snapshot_age',status='PASS'))
        # Reproduce old A01 decimal metadata using the pre-A03 writer settings.
        # Publication remains valid Delta, but Sail admission must fail closed.
        parent=self.state(parent,'running')
        legacy=self.root/'legacy.py'
        legacy.write_text(f"import sys\nsys.path.insert(0,{str(worker.parent)!r})\nimport export\noriginal=export.write_deltalake\ndef write(*a,**kw):\n kw.pop('configuration',None)\n return original(*a,**kw)\nexport.write_deltalake=write\nsys.exit(export.main())\n")
        (self.root/'session.py').symlink_to(worker.with_name('session.py'))
        self.configure(legacy);self.refresh()
        rejected=self.ready(self.api('analytics_open',branch='main',key=str(uuid.uuid4())),'failed')
        assert 'unsafe decimal statistics' in rejected['error'],rejected
        self.configure(worker);self.refresh()
        s=self.opened();self.raw(s,2);self.close(s)
        self.checks.append(dict(name='legacy_decimal_statistics_rejected_and_refresh_repairs',status='PASS'))
        app=self.create('orders-app')
        migration=Path(__file__).resolve().parents[2]/'examples/orders/migrations/001-orders.sql'
        self.sql(app,migration.read_text())
        self.sql(app,"INSERT INTO orders(customer,total_cents) VALUES('Ada',1299),('Ada',501)")
        s=self.ready(self.api('analytics_open',branch='orders-app',key=str(uuid.uuid4())))
        assert self.query(s,'SELECT customer,sum(total_cents) FROM public.orders GROUP BY customer')['rows']==[['Ada','1800']]
        self.close(s)
        self.checks.append(dict(name='orders_example_schema_queryable_without_changes',status='PASS'))
        self.stop()

    def session_records(self):
        return [p for p in self.records() if p['role'].startswith('analytics-session-')]

    def ready_after_expiry(self,s):
        wait(lambda:self.api('analytics_session',id=s['id'])['state'] in ('failed','closed'),timeout=20)
        assert not self.session_records()


def main():
    p=argparse.ArgumentParser()
    for name in ('binary','bundle','helpers','python','worker','report'):p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-a03-',dir='/tmp')).resolve()
    cell=Sessions(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
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
