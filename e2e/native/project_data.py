#!/usr/bin/env python3
"""PK08: real PG17 logical table portability through the installed CLI contract."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import psycopg
from psycopg import sql
from workflow import Workflow


def qualify(cell, report):
    cell.worktree=cell.root/'project';cell.worktree.mkdir()
    (cell.worktree/'query.sql').write_text('SELECT 1\n')
    (cell.worktree/'supabricks.toml').write_text(f'''format_version=2
id="{uuid.uuid4()}"
name="logical-data"
[package]
version="0.1.0"
include=["query.sql"]
notebook_outputs="strip"
[resources.database.main]
kind="postgres_database"
lifecycle="retain"
''')
    cell.cli('up','--bundle',cell.bundle,'--helpers',cell.helpers)
    cell.cli('project','create','--key','source')
    for branch in ('source','destination','atomic','retry'):
        cell.cli('database','create',branch,'--key',branch,'--wait')
    def connect(branch):
        return psycopg.connect(cell.cli('connect',branch)['uri'],autocommit=True)
    def selection(*names):
        path=cell.root/'tables.json'
        path.write_text(json.dumps(dict(version=1,tables=[dict(schema='public',name=n) for n in names])))
        return path
    def export(path,*names,code=0):
        return cell.cli('project','data','export','--branch','source','--tables',selection(*names),'--output',path,code=code)
    def check(name):
        report['checks'].append(name); print(name,flush=True)
    def reseal(value,path):
        for table in value['content']['tables']:
            table['data_sha256']=hashlib.sha256(table['copy_text'].encode()).hexdigest()
        # Rust's schema-ordered JSON is retained by Python's insertion order.
        content=json.dumps(value['content'],ensure_ascii=False,separators=(',',':')).encode()
        value['content_sha256']=hashlib.sha256(content).hexdigest()
        path.write_text(json.dumps(value,ensure_ascii=False,separators=(',',':')))
    with connect('source') as source:
        print('database locale:',source.execute('SELECT datcollate,datctype,datlocprovider,datlocale FROM pg_database WHERE datname=current_database()').fetchone(),flush=True)
        source.execute('CREATE TABLE public.first(id integer PRIMARY KEY, note text UNIQUE, data bytea, doc jsonb, value numeric(30,8), moment timestamptz, optional text)')
        source.execute('CREATE TABLE public.second(id integer NOT NULL, marker integer NOT NULL)')
        source.execute("INSERT INTO public.first VALUES(1,%s,%s,%s,12345678901234567890.12345678,'2020-02-03 04:05:06.123456+05:30',NULL)",('Unicode é\tline\n\\N',b'\x00\xff\x01','{"items":[1,null,"é"]}'))
        source.execute('INSERT INTO public.second VALUES(1,0)')
    package=cell.root/'sales.sbdata'
    exported=export(package,'first','second')
    assert exported['verified'] and len(exported['tables'])==2
    assert all('copy_text' not in t for t in exported['tables'])
    verified=subprocess.run([str(cell.binary),'project','data','verify',str(package)],env={'PATH':'/usr/bin:/bin'},cwd=cell.root,capture_output=True,text=True,check=True)
    assert json.loads(verified.stdout)['archive_sha256']==exported['archive_sha256']
    check('bounded_offline_archive_inspection')
    imported=cell.cli('project','data','import',package,'--branch','destination','--key','initial')
    assert imported['state']=='committed' and not imported['replayed']
    with connect('source') as source,connect('destination') as destination:
        for name in ('first','second'):
            query=sql.SQL('SELECT * FROM public.{} ORDER BY id').format(sql.Identifier(name))
            assert source.execute(query).fetchall()==destination.execute(query).fetchall()
        constraints=destination.execute("SELECT contype FROM pg_constraint WHERE conrelid='public.first'::regclass ORDER BY contype").fetchall()
        assert constraints==[('p',),('u',)]
        assert destination.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='public.first'::regclass").fetchone()==('supabricks_owner',)
    check('type_binary_null_constraint_and_owner_fidelity')
    with connect('source') as source:
        source.execute('CREATE TABLE public.all_types(a boolean,b smallint,c bigint,d real,e double precision,f date,g timestamp,h uuid,i json,j varchar(32),k numeric,l numeric(8,-2))')
        source.execute("INSERT INTO public.all_types VALUES(true,-32768,9223372036854775807,'Infinity','NaN','0001-01-01 BC','2020-02-03 04:05:06.123456','01234567-89ab-cdef-0123-456789abcdef','{\"a\": 1}','é','NaN',12345)")
    types=cell.root/'types.sbdata';export(types,'all_types')
    cell.cli('project','data','import',types,'--branch','destination','--key','types')
    with connect('source') as source,connect('destination') as destination:
        assert source.execute('SELECT all_types::text FROM public.all_types').fetchall()==destination.execute('SELECT all_types::text FROM public.all_types').fetchall()
    check('scalar_type_matrix_special_values_and_type_modifiers')
    odd='odd"; DROP TABLE first; --'
    with connect('source') as source:
        source.execute(sql.SQL('CREATE TABLE public.{} ({} integer)').format(sql.Identifier(odd),sql.Identifier('quote" é')))
        source.execute(sql.SQL('INSERT INTO public.{} VALUES(42)').format(sql.Identifier(odd)))
    names=cell.root/'quoted.sbdata';export(names,odd)
    cell.cli('project','data','import',names,'--branch','destination','--key','quoted')
    with connect('destination') as destination:
        assert destination.execute(sql.SQL('SELECT * FROM public.{}').format(sql.Identifier(odd))).fetchone()==(42,)
        assert destination.execute('SELECT count(*) FROM public.first').fetchone()==(1,)
    check('quoted_identifiers_remain_identifiers_not_executable_sql')
    replay=cell.cli('project','data','import',package,'--branch','destination','--key','initial')
    assert replay['replayed'] and replay['tables']==imported['tables']
    assert cell.cli('project','data','status','--branch','destination','--key','initial')['state']=='committed'
    cell.cli('project','data','import',package,'--branch','destination','--key','different',code=4)
    check('idempotent_receipt_and_existing_table_refusal')
    cell.cli('project','data','import',package,'--branch','atomic','--key','initial')
    with connect('atomic') as destination:
        destination.execute('DROP TABLE public.first')
    cell.cli('project','data','import',package,'--branch','atomic','--key','initial',code=4)
    check('removed_destination_identity_not_silently_reused')
    # A late data failure must roll back the first table too, including receipt.
    broken=json.loads(package.read_text());broken['content']['tables'][1]['copy_text']='invalid\t0\n'
    bad=cell.root/'invalid-row.sbdata';reseal(broken,bad)
    cell.cli('project','data','import',bad,'--branch','retry','--key','rollback',code=6)
    assert cell.cli('project','data','status','--branch','retry','--key','rollback')['state']=='not_committed'
    with connect('retry') as destination:
        assert destination.execute("SELECT to_regclass('public.first'),to_regclass('public.second')").fetchone()==(None,None)
    check('late_failure_rolls_back_entire_table_set_and_receipt')
    # Deterministic SIGKILL windows establish rollback versus committed replay.
    def kill_import(point):
        p=subprocess.run([str(cell.binary),'project','data','import',str(package),'--branch','retry','--key','crash','--project',str(cell.worktree),'--data-dir',str(cell.root)],env=dict(cell.env,SUPABRICKS_TEST_LOGICAL_DATA_KILL=point),capture_output=True,timeout=120)
        assert p.returncode==-9
    kill_import('before_commit')
    assert cell.cli('project','data','status','--branch','retry','--key','crash')['state']=='not_committed'
    kill_import('after_commit')
    assert cell.cli('project','data','status','--branch','retry','--key','crash')['state']=='committed'
    assert cell.cli('project','data','import',package,'--branch','retry','--key','crash')['replayed']
    check('sigkill_before_and_after_commit_reconciles_without_duplicate_rows')
    changed=json.loads(package.read_text());changed['content']['tables'][1]['copy_text']='1\t1\n'
    other=cell.root/'other.sbdata';reseal(changed,other)
    cell.cli('project','data','import',other,'--branch','retry','--key','crash',code=4)
    locale=json.loads(package.read_text());locale['content']['locale']['version']='unsupported-version'
    other=cell.root/'locale.sbdata';reseal(locale,other)
    cell.cli('project','data','import',other,'--branch','retry','--key','locale',code=2)
    check('changed_archive_key_and_locale_mismatch_refused')
    other_project=cell.root/'second-project';shutil.copytree(cell.worktree,other_project)
    cell.cli('project','data','import',package,'--branch','destination','--key','other',project=other_project,code=4)
    cell.cli('project','create','--key','other-deployment',project=other_project)
    cell.cli('database','create','destination','--wait',project=other_project)
    other_import=cell.cli('project','data','import',package,'--branch','destination','--key','initial',project=other_project)
    assert other_import['destination_project_id']!=imported['destination_project_id']
    assert other_import['destination_branch_id']!=imported['destination_branch_id']
    assert other_import['source']==imported['source']
    check('new_deployment_owns_fresh_ids_with_source_provenance_preserved')
    with connect('source') as source:
        source.execute('CREATE TABLE public.snapshot_a(value integer NOT NULL)');source.execute('INSERT INTO public.snapshot_a VALUES(0)')
        source.execute('CREATE TABLE public.snapshot_b(value integer NOT NULL)');source.execute('INSERT INTO public.snapshot_b VALUES(0)')
    stop=threading.Event();errors=[];commits=[];uri=cell.cli('connect','source')['uri']
    def writer():
        try:
            with psycopg.connect(uri,autocommit=True) as connection:
                while not stop.is_set():
                    with connection.transaction():
                        connection.execute('UPDATE public.snapshot_a SET value=value+1')
                        connection.execute('UPDATE public.snapshot_b SET value=value+1')
                    commits.append(1);time.sleep(.001)
        except Exception as error: errors.append(type(error).__name__)
    thread=threading.Thread(target=writer);thread.start()
    try:
        for i in range(5):
            path=cell.root/f'snapshot-{i}.sbdata';export(path,'snapshot_a','snapshot_b')
            tables=json.loads(path.read_text())['content']['tables']
            assert tables[0]['copy_text']==tables[1]['copy_text']
    finally:stop.set();thread.join(timeout=30)
    assert not thread.is_alive() and not errors and commits
    check('multi_table_snapshot_consistency_under_concurrent_commits')
    with connect('source') as source:
        for name,definition in [('serial_value','id serial'),('default_value','id int DEFAULT 3'),('check_value','id int CHECK(id>0)'),('array_value','value int[]'),('generated_value','id int, g int GENERATED ALWAYS AS(id+1) STORED')]:
            source.execute(sql.SQL('CREATE TABLE public.{} ({})').format(sql.Identifier(name),sql.SQL(definition)))
            path=cell.root/f'{name}.sbdata';export(path,name,code=2);assert not path.exists()
        source.execute('CREATE TABLE public.indexed(id int)');source.execute('CREATE INDEX ON public.indexed(id)')
        path=cell.root/'indexed.sbdata';export(path,'indexed',code=2);assert not path.exists()
    check('unsupported_schema_rejected_without_archive_publication')
    # Probe the off-the-shelf alternative: pg_dump preserves sequence/default
    # semantics, but carries executable schema SQL rather than our typed profile.
    target=cell.cli('connect','source')
    dump=subprocess.run([str(cell.bundle/'pg_install/v17/bin/pg_dump'),'-h','127.0.0.1','-p',str(target['port']),'-U','supabricks_owner','-d','postgres','--schema-only','--table=public.serial_value','--no-owner','--no-acl'],env=dict(cell.env,PGPASSWORD=target['password']),capture_output=True,text=True,check=True,timeout=60)
    assert 'CREATE SEQUENCE' in dump.stdout and 'nextval(' in dump.stdout
    check('pg_dump_schema_fidelity_probe_records_executable_dump_boundary')
    with connect('source') as source:
        source.execute('CREATE TABLE public.large_rows(value text)')
        source.execute("INSERT INTO public.large_rows SELECT repeat('x',100000) FROM generate_series(1,340)")
        path=cell.root/'too-large.sbdata';export(path,'large_rows',code=2);assert not path.exists()
        source.execute('TRUNCATE public.large_rows');source.execute("INSERT INTO public.large_rows VALUES(repeat('x',300000))")
        path=cell.root/'oversize-row.sbdata';export(path,'large_rows',code=2);assert not path.exists()
    check('data_and_row_limits_leave_no_archive_or_staging_directory')
    assert not list(cell.root.glob('.supabricks-package-*'))
    # The release closure exporter uses a legacy project created by `init`.
    legacy=cell.root/'legacy';legacy.mkdir()
    cell.cli('init','logical-legacy',project=legacy)
    cell.cli('database','create','main','--key','legacy-source','--wait',project=legacy)
    cell.cli('sql','--branch','main','--write','--sql','CREATE TABLE public.portable_sales(id integer PRIMARY KEY, amount numeric(18,2), payload bytea)',project=legacy)
    cell.cli('sql','--branch','main','--write','--sql',"INSERT INTO public.portable_sales VALUES(1,10,decode('00ff','hex')),(2,20,NULL)",project=legacy)
    logical=cell.root/'legacy.sbdata'
    produced=cell.cli('project','data','export','--branch','main','--tables',selection('portable_sales'),'--output',logical,project=legacy)
    # Match the release consumer's offline invocation (data-dir, no project).
    verified=subprocess.run([str(cell.binary),'project','data','verify',str(logical),'--data-dir',str(cell.root)],env=cell.env,capture_output=True,text=True,check=True)
    assert json.loads(verified.stdout)==produced
    cell.cli('project','data','import',logical,'--branch','destination','--key','legacy-data')
    assert cell.cli('sql','--branch','destination','--sql',"SELECT count(*),sum(amount),min(encode(payload,'hex')) FROM public.portable_sales")['rows']==[['2','30.00','00ff']]
    check('legacy_source_exports_release_fixture_into_format_two_destination')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',type=Path,required=True);parser.add_argument('--bundle',type=Path,required=True);parser.add_argument('--helpers',type=Path,required=True);parser.add_argument('--report',type=Path,required=True)
    args=parser.parse_args();root=Path(tempfile.mkdtemp(prefix='sb-pk08-',dir='/tmp')).resolve()
    cell=Workflow(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    report=dict(status='failed',checks=[])
    try:
        qualify(cell,report);report['status']='passed'
    finally:
        try:cell.cli('down')
        except Exception:
            report['status']='failed';report['cleanup_failed']=True
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,indent=2)+'\n')
        if report['status']=='passed':shutil.rmtree(root)
        else:print('Private failed fixture:',root,flush=True)
    assert report['status']=='passed'


if __name__=='__main__':main()
