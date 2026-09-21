"""Restricted roles on two real pinned Neon/PG17 branch computes."""
import secrets
import threading
import time
import psycopg
from common import wait


def probe(cell, root, evidence):
    (cell.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{cell.project}"\nname = "iam00"\n')
    cell.request(method='resolve_binding',source=dict(definition_id=cell.project,worktree=str(cell.work)))
    alice,bob=cell.create('alice'),cell.create('bob')
    password=secrets.token_hex(24)
    cell.sql(alice, "CREATE ROLE iam00_reader LOGIN PASSWORD '"+password+"' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; "
        "CREATE TABLE allowed_data(id int); INSERT INTO allowed_data VALUES(42); "
        "CREATE TABLE denied_data(secret text); INSERT INTO denied_data VALUES('private'); "
        "GRANT CONNECT ON DATABASE postgres TO iam00_reader; GRANT USAGE ON SCHEMA public TO iam00_reader; "
        "GRANT SELECT ON allowed_data TO iam00_reader")
    def connect(branch=alice,user='iam00_reader'):
        return psycopg.connect(host='127.0.0.1',port=branch['ports']['sql'],dbname='postgres',
            user=user,password=password,connect_timeout=2,autocommit=True)
    with connect() as db:
        evidence.check('pg_restricted_positive_control',db.execute('SELECT id FROM allowed_data').fetchone()==(42,))
        denied={
            'other_table':'SELECT * FROM denied_data',
            'control_role':'SET ROLE cloud_admin',
            'owner_role':'SET ROLE supabricks_owner',
            'server_file':"SELECT pg_read_file('/etc/passwd')",
            'server_program':"COPY allowed_data TO PROGRAM 'true'",
            'superuser':'ALTER ROLE iam00_reader SUPERUSER',
            'create_role':'CREATE ROLE iam00_escalation SUPERUSER',
            'grant_read_all':'GRANT pg_read_all_data TO iam00_reader',
            'replication':'ALTER ROLE iam00_reader REPLICATION',
            'bypass_rls':'ALTER ROLE iam00_reader BYPASSRLS',
        }
        for name,sql in denied.items():
            try: db.execute(sql)
            except psycopg.Error as error:
                state=error.sqlstate
                evidence.check('pg_denies_'+name,state=='42501',sqlstate=state)
            else: raise AssertionError('PG unexpectedly allowed '+name)
        for branch,user,name in [(bob,'iam00_reader','other_branch'),(alice,'cloud_admin','control_login'),(alice,'supabricks_owner','owner_login')]:
            try:
                with connect(branch,user): pass
            except psycopg.OperationalError as error:
                assert 'password authentication failed' in str(error), 'connection failed for a non-authentication reason'
                assert cell.sql(branch,'SELECT 1')=='1', 'negative connection target is unavailable'
                evidence.check('pg_denies_'+name,True)
            else: raise AssertionError('PG unexpectedly accepted '+name)
    # An administrative supervisor must terminate existing work, not just revoke CONNECT.
    db=connect(); pid=db.info.backend_pid; completed=threading.Event(); outcome=[]
    def long_query():
        try: db.execute('SELECT pg_sleep(120)')
        except psycopg.Error as error: outcome.append(error.sqlstate)
        finally: completed.set()
    thread=threading.Thread(target=long_query,daemon=True); thread.start()
    try:
        wait(lambda: cell.sql(alice,f"SELECT state FROM pg_stat_activity WHERE pid={pid}")=='active',10)
        started=time.monotonic()
        cell.sql(alice,f'SELECT pg_terminate_backend({pid})')
        assert completed.wait(10), 'running PG query survived terminate'
        elapsed=time.monotonic()-started
        evidence.check('pg_running_query_terminated',elapsed<60 and outcome==['57P01'],seconds=round(elapsed,3))
        evidence.metrics['pg_termination_seconds']=round(elapsed,3)
    finally:
        db.close(); thread.join(timeout=5)
    return [alice['ports']['sql'],bob['ports']['sql']]
