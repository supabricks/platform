"""I03 installed database fidelity, snapshot compatibility and fault qualification."""
import datetime as dt
from decimal import Decimal
import hashlib
import json
import time
import pyarrow as pa
import pyarrow.parquet as pq
import psycopg


def qualify_formats(workspace, cli, check, wait, owned, terminal, report):
    def sql(query):return cli('sql','--branch','main','--sql',query)['rows']
    def write(query):cli('sql','--branch','main','--sql',query,'--write')
    def inspect(path,format=None):
        v=cli('ingest','inspect',path,*(['--format',format] if format else []))
        p=workspace/(path.name+'.mapping.json');p.write_text(json.dumps(v['inspection']['mapping']))
        return v,p
    def load(v,m,table,waited=True,ok=True):
        return cli('ingest','load','--source',v['source']['id'],'--schema-file',m,'--branch','main','--table',table,'--key',table,*(['--wait'] if waited else []),ok=ok)
    raw='{"id":9007199254740993,"zip":"001","amount":12345678901234567890.1234567890}'
    paths=[]
    for extension,content in [('jsonl',raw+'\n'),('json','['+raw+']')]:
        p=workspace/('i03.'+extension);p.write_text(content);paths.append((extension,p,None))
    p=workspace/'i03.parquet';pq.write_table(pa.table({'id':pa.array([9007199254740993],type=pa.int64()),'zip':['001'],
        'amount':pa.array([Decimal('12345678901234567890.1234567890')],type=pa.decimal128(30,10))}),p);paths.append(('parquet',p,None))
    expected=[['9007199254740993','001','12345678901234567890.1234567890']]
    report['formats']={}
    for name,path,format in paths:
        digest=hashlib.sha256(path.read_bytes()).hexdigest();view,m=inspect(path,format)
        job=load(view,m,'i03_'+name)
        assert job['state']=='succeeded' and job['committed_rows']==1,job
        assert sql(f'SELECT * FROM i03_{name}')==expected
        assert load(view,m,'i03_'+name)['id']==job['id']
        assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
        report['formats'][name]=dict(source_sha256=digest,source_bytes=path.stat().st_size,metrics=job['metrics'])
        assert 0<job['metrics']['peak_rss_bytes']<=512*1024**2
        cli('analytics','refresh','--branch','main','--wait')
        assert cli('analytics','sql','--branch','main','--sql',f'SELECT * FROM public.i03_{name}')['rows']==expected
        check(f'{name}: real COPY preserves scalar values, idempotent replay and source bytes; published Sail snapshot matches')
    p=workspace/'i03-document.json';p.write_text('{"rows":[{"n":9007199254740993,"amount":1.234567890123456789},null]}')
    paths.append(('document',p,'json_document'))
    view,m=inspect(p,'json_document');job=load(view,m,'i03_document')
    report['formats']['document']=dict(source_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),source_bytes=p.stat().st_size,metrics=job['metrics'])
    assert sql("SELECT document->'rows'->0->>'amount' FROM i03_document")==[['1.234567890123456789']]
    failed=cli('analytics','refresh','--branch','main','--wait',ok=False)
    assert failed['state'] in ('failed','cancelled'),failed
    # Previously published epochs remain readable when a jsonb export is refused.
    assert cli('analytics','sql','--branch','main','--sql','SELECT count(*) FROM public.i03_parquet')['rows']==[['1']]
    write('DROP TABLE i03_document')
    check('document jsonb preserves exact nested numbers; unsupported analytical refresh fails without replacing the old snapshot')

    nested=workspace/'i03-nested.jsonl';nested.write_text('{"id":1,"meta":{"x":[1,null]}}\n{"id":2,"meta":null}\n{"id":3}\n')
    v,m=inspect(nested);load(v,m,'i03_nested')
    assert sql("SELECT id,meta::text,meta IS NULL FROM i03_nested ORDER BY id")==[['1','{"x": [1, null]}','f'],['2','null','f'],['3',None,'t']]
    check('JSON missing keys become SQL NULL while explicit JSON null and nested structure survive in jsonb')
    write('DROP TABLE i03_nested')

    p=workspace/'i03-types.parquet';pq.write_table(pa.table({'u':pa.array([2**64-1],type=pa.uint64()),
        't':pa.array([dt.datetime(2020,1,1,tzinfo=dt.timezone.utc)],type=pa.timestamp('us',tz='America/Chicago'))}),p)
    v,m=inspect(p);assert 'America/Chicago' in v['inspection']['source_schema'][1]['arrow_type']
    load(v,m,'i03_types')
    assert sql("SELECT u, extract(epoch from t)::bigint FROM i03_types")==[['18446744073709551615','1577836800']]
    check('Parquet uint64 widens to exact numeric and timezone-aware values preserve their instant and source timezone metadata')

    # Hold only this fixture's receipt table. COPY finishes but cannot insert its
    # receipt/commit; this creates a deterministic real transaction fault boundary
    # even for one-row document imports, without production test hooks.
    for name,path,format in paths:
        for action in ('kill','cancel'):
            v,m=inspect(path,format)
            table=f'i03_{name}_{action}'
            with psycopg.connect(cli('connect','main')['uri']) as blocker:
                blocker.execute('LOCK TABLE _supabricks.ingest_receipts IN SHARE MODE')
                job=load(v,m,table,False)
                wait(lambda: blocker.execute("SELECT EXISTS(SELECT FROM pg_locks WHERE relation='_supabricks.ingest_receipts'::regclass AND NOT granted)").fetchone()[0],20)
                assert sql(f"SELECT to_regclass('{table}')")==[[None]]
                if action=='kill':owned('ingest-'+job['id']).kill()
                else:cli('ingest','cancel',job['id'],'--wait')
            stopped=terminal(job['id'])
            assert stopped['state']==('failed' if action=='kill' else 'cancelled'),stopped
            assert sql(f"SELECT to_regclass('{table}')")==[[None]]
            if action=='kill':
                retried=cli('ingest','retry',job['id'],'--wait')
                assert retried['id']==job['id'] and retried['attempt']==2 and retried['committed_rows']==1,retried
                write(f'DROP TABLE {table}')
            check(f'{name}: {action} after real COPY rolls back table and receipt'+('; explicit retry commits exactly once' if action=='kill' else ''))

    for extension in ('jsonl','json'):
        p=workspace/('i03-late.'+extension)
        records=['{"id":1}']*110+['{"id":2,"unexpected":true}']
        p.write_text('\n'.join(records)+'\n' if extension=='jsonl' else '['+','.join(records)+']')
        v,m=inspect(p);job=load(v,m,'i03_late_'+extension,ok=False)
        assert job['state']=='failed' and job['error']=='unmapped_json_key',job
        assert sql(f"SELECT to_regclass('i03_late_{extension}')")==[[None]]
        check(f'{extension}: a late unmapped key rejects the whole transaction after a valid preview')
    p=workspace/'i03-late.parquet';pq.write_table(pa.table({'n':pa.array([1]*110+[2**64-1],type=pa.uint64())}),p,row_group_size=16)
    v,m=inspect(p);mapping=json.loads(m.read_text());mapping['columns'][0]['data_type']={'kind':'bigint'};m.write_text(json.dumps(mapping))
    job=load(v,m,'i03_late_parquet',ok=False)
    assert job['state']=='failed' and job['error']=='integer_overflow' and sql("SELECT to_regclass('i03_late_parquet')")==[[None]],job
    check('Parquet unsigned overflow under an explicitly narrowed mapping rolls back the entire table')
    p=workspace/'i03-corrupt.parquet'
    pq.write_table(pa.table({'n':list(range(300))}),p,row_group_size=100,compression='NONE',use_dictionary=False,write_page_checksum=True)
    offset=pq.ParquetFile(p).metadata.row_group(2).column(0).data_page_offset
    raw=bytearray(p.read_bytes());raw[offset+100]^=1;p.write_bytes(raw)
    v,m=inspect(p);job=load(v,m,'i03_corrupt',ok=False)
    assert job['state']=='failed' and job['error']=='invalid_parquet' and sql("SELECT to_regclass('i03_corrupt')")==[[None]],job
    check('Parquet corruption beyond the preview is detected by page checksum and rolls back COPY')
    # Nontrivial compressed/decoded and whole-JSON inputs, with real COPY and
    # measured worker RSS. Keep this distinct from the parser's injected bounds.
    payload='x'*512
    budget_paths=[]
    p=workspace/'i03-budget.jsonl';p.write_text(('{"payload":"'+payload+'"}\n')*8192);budget_paths.append(('jsonl',p,None,8192))
    p=workspace/'i03-budget.json';p.write_text('['+','.join(['{"payload":"'+payload+'"}']*8192)+']')
    with p.open('ab') as f:f.write(b' '*(10*1024**2-p.stat().st_size))
    budget_paths.append(('json',p,None,8192))
    p=workspace/'i03-budget-document.json';p.write_text('{"payload":"'+('x'*(4*1024**2))+'"}')
    with p.open('ab') as f:f.write(b' '*(10*1024**2-p.stat().st_size))
    budget_paths.append(('document',p,'json_document',1))
    p=workspace/'i03-budget.parquet';pq.write_table(pa.table({'payload':[payload]*32768}),p,row_group_size=1024)
    budget_paths.append(('parquet',p,None,32768))
    for name,path,format,count in budget_paths:
        v,m=inspect(path,format);started=time.monotonic();job=load(v,m,'i03_budget_'+name)
        metrics=job['metrics'];assert job['committed_rows']==count and 0<metrics['peak_rss_bytes']<=512*1024**2 and metrics['decoded_bytes']<=512*1024**2,job
        report['formats'][name]['budget']=dict(source_bytes=path.stat().st_size,source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),rows=count,elapsed_seconds=round(time.monotonic()-started,3),**metrics)
        write('DROP TABLE i03_budget_'+name)
    check('all formats complete nontrivial COPY within sampled RSS/decoded ceilings; JSON array and document accept the exact 10 MiB source limit')
    for name,raw,format,code in [('json-large',b' '* (10*1024**2+1),'json_array','json_source_limit'),
                                ('json-broken',b'[{"id":1}','json_array','invalid_source'),
                                ('parquet-broken',b'PAR1garbage','parquet','invalid_parquet_footer')]:
        p=workspace/name;p.write_bytes(raw)
        rejected=cli('ingest','inspect',p,'--format',format,ok=False)
        assert rejected['error']==code,rejected
    check('real service rejects oversized JSON and truncated JSON/Parquet before any table admission')
