"""Owned pgoutput resources and whole-group qualification on the native PG17 source."""
import json
import time
import psycopg
from psycopg import sql
from .spool import CaptureError, canonical, lsn, pg_lsn


def connect(config):
    return psycopg.connect(host=config['socket_dir'],port=config['port'],dbname='postgres',user='cloud_admin',
        autocommit=True,connect_timeout=3,application_name='supabricks-capture-control',
        options='-c statement_timeout=3000 -c lock_timeout=1000 -c search_path=pg_catalog -c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1 -c temp_file_limit=65536 -c logical_decoding_work_mem=1024')


def inspect(conn,identity):
    actual=conn.execute("SELECT current_setting('neon.tenant_id'),current_setting('neon.timeline_id'),oid FROM pg_database WHERE datname=current_database()").fetchone()
    if list(actual[:2])!=[identity['tenant_id'],identity['timeline_id']]:raise CaptureError('source_identity_changed')
    settings=dict(conn.execute("SELECT name,setting FROM pg_settings WHERE name=ANY(%s)",(['server_version_num','wal_level','fsync','max_prepared_transactions'],)).fetchall())
    if not settings['server_version_num'].startswith('17') or settings['wal_level']!='logical' or settings['fsync']!='on' or settings['max_prepared_transactions']!='0':raise CaptureError('unsupported_source_settings')
    relations=conn.execute("""SELECT c.oid,n.nspname,c.relname,c.relkind,c.relpersistence,c.relrowsecurity,c.relispartition,c.relreplident,
        c.relowner='cloud_admin'::regrole,EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid),
        EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%%'
        AND n.nspname NOT IN ('information_schema','_supabricks') AND c.relkind IN ('r','p','f','m') ORDER BY c.oid LIMIT 131""").fetchall()
    expected={};controls=[]
    for oid,namespace,name,kind,persistence,rls,partition,identity_mode,engine_owner,inherits,extension in relations:
        if (namespace,name) in [('public','health_check'),('neon_migration','migration_id')]:
            if not engine_owner or kind!='r' or rls:raise CaptureError('engine_object_identity')
            controls.append(oid);continue
        if namespace!='public' or kind!='r' or persistence!='p' or rls or partition or identity_mode!='d' or inherits or extension:raise CaptureError('unsupported_source_relation')
        keys=conn.execute('SELECT indkey::smallint[] FROM pg_index WHERE indrelid=%s AND indisprimary AND indisvalid',(oid,)).fetchall()
        if len(keys)!=1 or len(keys[0][0])!=1:raise CaptureError('integer_primary_key_required')
        key=keys[0][0][0]
        columns=conn.execute('SELECT attnum,attname,atttypid,atttypmod,attnotnull,attgenerated,attcollation FROM pg_attribute WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum LIMIT 129',(oid,)).fetchall()
        if not 1<=len(columns)<=128:raise CaptureError('column_budget')
        profile=[]
        for num,column,typ,mod,notnull,generated,collation in columns:
            if generated or collation not in (0,100) or typ not in (20,21,23,25,1043,1700):raise CaptureError('unsupported_source_column')
            if typ==1700:
                precision=((mod-4)>>16)&65535;scale=(mod-4)&2047;scale=scale-2048 if scale>=1024 else scale
                if mod<4 or not 1<=precision<=38 or not 0<=scale<=precision:raise CaptureError('unsupported_decimal')
            if num==key and (typ not in (20,21,23) or not notnull):raise CaptureError('integer_primary_key_required')
            profile.append([int(num==key),column,typ,mod])
        expected[str(oid)]=[namespace,name,identity_mode,profile]
    if not 1<=len(expected)<=128:raise CaptureError('table_budget')
    result=dict(database_oid=actual[2],relations=expected,engine_objects=controls)
    if len(canonical(result))>128*1024:raise CaptureError('schema_budget')
    return result


class Source:
    def __init__(self,config,spool):
        self.config=config;self.spool=spool;self.conn=connect(config)
        self.name='sbcap_'+config['identity']['generation'].replace('-','')
        self.slot=self.name;self.publication=self.name;self.schema=self.name
        self.fence='supabricks.capture.'+config['identity']['generation']
        self.owner=canonical(config['identity']).decode()
    def close(self):self.conn.close()
    def profile(self):return inspect(self.conn,self.config['identity'])
    def established(self):
        profile=self.profile()
        previous=self.spool.get('profile')
        if previous is not None and profile!=previous:raise CaptureError('schema_changed')
        return profile
    def setup(self):
        profile=self.established()
        foreign=self.conn.execute("SELECT slot_name FROM pg_replication_slots WHERE slot_name<>%s AND NOT (slot_name='wal_proposer_slot' AND slot_type='physical' AND plugin IS NULL AND database IS NULL) LIMIT 1",(self.slot,)).fetchone()
        if foreign:raise CaptureError('unmanaged_replication_resources')
        pub=self.conn.execute('SELECT obj_description(oid,\'pg_publication\'),pubowner=\'cloud_admin\'::regrole FROM pg_publication WHERE pubname=%s',(self.publication,)).fetchone()
        if pub and pub!=(self.owner,True):raise CaptureError('capture_resource_collision')
        if not pub:
            if self.spool.get('start') is not None:raise CaptureError('capture_resources_missing')
            existing=self.conn.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(self.schema,)).fetchone()
            if existing:raise CaptureError('capture_resource_collision')
            with self.conn.transaction():
                self.conn.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION cloud_admin').format(sql.Identifier(self.schema)))
                self.conn.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(self.schema)))
                self.conn.execute(sql.SQL('COMMENT ON SCHEMA {} IS {}').format(sql.Identifier(self.schema),sql.Literal(self.owner)))
                tables=sql.SQL(',').join(sql.Identifier(v[0],v[1]) for v in profile['relations'].values())
                self.conn.execute(sql.SQL('CREATE PUBLICATION {} FOR TABLE {}').format(sql.Identifier(self.publication),tables))
                self.conn.execute(sql.SQL('COMMENT ON PUBLICATION {} IS {}').format(sql.Identifier(self.publication),sql.Literal(self.owner)))
                # Fixed captured engine OIDs, not user-controlled name filtering.
                controls=profile['engine_objects'] or [0]
                body=sql.SQL("""CREATE FUNCTION {}.fence() RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $f$
                DECLARE relevant boolean; BEGIN
                  IF TG_EVENT='sql_drop' THEN
                    SELECT EXISTS(SELECT 1 FROM pg_event_trigger_dropped_objects() WHERE schema_name NOT LIKE 'pg_%%'
                        AND schema_name NOT IN ('information_schema','_supabricks',{}) AND objid<>ALL({}::oid[])
                        AND object_type IN ('table','table column','index','schema')) INTO relevant;
                  ELSE
                    SELECT EXISTS(SELECT 1 FROM pg_event_trigger_ddl_commands() WHERE schema_name NOT LIKE 'pg_%%'
                        AND schema_name NOT IN ('information_schema','_supabricks',{}) AND objid<>ALL({}::oid[])
                        AND object_type IN ('table','table column','index','schema')) INTO relevant;
                  END IF;
                  IF relevant THEN PERFORM pg_logical_emit_message(true,{},TG_TAG); END IF;
                END $f$""").format(sql.Identifier(self.schema),sql.Literal(self.schema),sql.Literal(controls),sql.Literal(self.schema),sql.Literal(controls),sql.Literal(self.fence))
                self.conn.execute(body)
                self.conn.execute(sql.SQL('CREATE EVENT TRIGGER {} ON ddl_command_end EXECUTE FUNCTION {}.fence()').format(sql.Identifier(self.name+'_end'),sql.Identifier(self.schema)))
                self.conn.execute(sql.SQL('CREATE EVENT TRIGGER {} ON sql_drop EXECUTE FUNCTION {}.fence()').format(sql.Identifier(self.name+'_drop'),sql.Identifier(self.schema)))
            # Setup commits before the logical slot, so its own DDL isn't in the feed.
        self.verify_resources(profile)
        self.spool.set('profile',profile)
        # This hard server safeguard persists even if the daemon/worker is offline.
        # It is checkpoint-enforced by PG, not an exact physical WAL byte ceiling.
        self.conn.execute(sql.SQL('ALTER SYSTEM SET max_slot_wal_keep_size = {}').format(sql.Literal(str(self.config['wal_bytes']//1024)+'kB')))
        self.conn.execute('SELECT pg_reload_conf()')
        deadline=time.monotonic()+3
        while int(self.conn.execute("SELECT setting FROM pg_settings WHERE name='max_slot_wal_keep_size'").fetchone()[0])*1024*1024!=self.config['wal_bytes']:
            if time.monotonic()>deadline:raise CaptureError('wal_retention_setting')
            time.sleep(.05)
        slot=self.slot_status()
        if slot is None:
            if self.spool.get('start') is not None:raise CaptureError('source_history_lost')
            slot_name,start=self.conn.execute('SELECT * FROM pg_create_logical_replication_slot(%s,\'pgoutput\')',(self.slot,)).fetchone()
            start=lsn(start)
        else:
            if self.spool.get('start') is not None:start=self.spool.get('start')
            else:start=lsn(slot['confirmed'])
        self.spool.establish(start,profile['relations'])
        return profile
    def slot_status(self):
        row=self.conn.execute("SELECT plugin,database,confirmed_flush_lsn::text,restart_lsn::text,wal_status,invalidation_reason,active,pg_current_wal_flush_lsn()::text FROM pg_replication_slots WHERE slot_name=%s",(self.slot,)).fetchone()
        if row is None:return None
        plugin,database,confirmed,restart,status,reason,active,source=row
        if plugin!='pgoutput' or database!='postgres' or reason or status=='lost' or restart is None or confirmed is None:raise CaptureError('source_history_lost')
        return dict(confirmed=confirmed,restart=restart,source=source,wal_status=status,active=active,retained_bytes=max(0,lsn(source)-lsn(restart)))
    def check(self):
        cap=int(self.conn.execute("SELECT setting FROM pg_settings WHERE name='max_slot_wal_keep_size'").fetchone()[0])*1024*1024
        if cap!=self.config['wal_bytes']:raise CaptureError('wal_retention_setting')
        slot=self.slot_status()
        if slot is None:raise CaptureError('source_history_lost')
        if lsn(slot['confirmed'])>self.spool.captured:raise CaptureError('source_ack_ahead_of_spool')
        if slot['retained_bytes']>=self.config['wal_bytes']*4//5:raise CaptureError('wal_budget')
        if self.established()!=self.spool.get('profile'):raise CaptureError('schema_changed')
        self.verify_resources(self.spool.get('profile'))
        triggers=self.conn.execute('SELECT count(*) FROM pg_event_trigger WHERE evtname=ANY(%s) AND evtenabled=\'O\'',([self.name+'_end',self.name+'_drop'],)).fetchone()[0]
        if triggers!=2:raise CaptureError('schema_fence_missing')
        return slot
    def verify_resources(self,profile):
        pub=self.conn.execute("SELECT oid,obj_description(oid,'pg_publication'),pubowner='cloud_admin'::regrole,puballtables,pubinsert,pubupdate,pubdelete,pubtruncate,pubviaroot FROM pg_publication WHERE pubname=%s",(self.publication,)).fetchone()
        if pub is None or pub[1:]!=(self.owner,True,False,True,True,True,True,False):raise CaptureError('capture_publication_changed')
        members=self.conn.execute('SELECT prrelid,prattrs IS NULL,prqual IS NULL FROM pg_publication_rel WHERE prpubid=%s',(pub[0],)).fetchall()
        if {str(r[0]) for r in members}!=profile['relations'].keys() or any(not r[1] or not r[2] for r in members):raise CaptureError('capture_publication_changed')
    def cleanup(self):
        identity=self.config['identity']
        actual=self.conn.execute("SELECT current_setting('neon.tenant_id'),current_setting('neon.timeline_id')").fetchone()
        if list(actual)!=[identity['tenant_id'],identity['timeline_id']]:raise CaptureError('source_identity_changed')
        # Ownership is proven by a transactional publication marker before any DROP.
        pub=self.conn.execute("SELECT obj_description(oid,'pg_publication'),pubowner='cloud_admin'::regrole FROM pg_publication WHERE pubname=%s",(self.publication,)).fetchone()
        if pub is None:
            if self.conn.execute('SELECT 1 FROM pg_replication_slots WHERE slot_name=%s',(self.slot,)).fetchone():raise CaptureError('capture_resource_collision')
            return
        if pub!=(self.owner,True):raise CaptureError('capture_resource_collision')
        schema=self.conn.execute("SELECT obj_description(oid,'pg_namespace'),nspowner='cloud_admin'::regrole FROM pg_namespace WHERE nspname=%s",(self.schema,)).fetchone()
        if schema!=(self.owner,True):raise CaptureError('capture_resource_collision')
        row=self.conn.execute('SELECT active FROM pg_replication_slots WHERE slot_name=%s',(self.slot,)).fetchone()
        if row:
            if row[0]:raise CaptureError('capture_slot_active')
            self.conn.execute('SELECT pg_drop_replication_slot(%s)',(self.slot,))
        with self.conn.transaction():
            for suffix in ('_end','_drop'):
                self.conn.execute(sql.SQL('DROP EVENT TRIGGER IF EXISTS {}').format(sql.Identifier(self.name+suffix)))
            self.conn.execute(sql.SQL('DROP PUBLICATION {}').format(sql.Identifier(self.publication)))
            self.conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
