#!/usr/bin/env python3
"""PG253: real extension catalogue, dependencies, branch/restart and API boundaries."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from cell import Cell, wait

MANAGED = ('pg_stat_statements', 'pgcrypto', 'pg_trgm', 'uuid-ossp', 'citext')

class Extensions(Cell):
    def api(self, command, binding=None):
        return self.request(method='api', api_version=1,
            binding=binding or dict(project_id=self.project, worktree=str(self.work)),
            action=dict(action='extensions', command=command))

    def target(self, branch):
        b = self.request(method='branch', id=branch['branch']['id'])
        return dict(branch=b['branch']['id'], revision=b['revision'])

    def catalogue(self, branch):
        return self.api(dict(action='list', target=self.target(branch)))

    def set(self, branch, name, enabled):
        return self.api(dict(action='set', target=self.target(branch), name=name, enabled=enabled))

    def refused(self, action, reason):
        try:
            action()
        except RuntimeError as error:
            assert reason in str(error), str(error)
        else:
            raise AssertionError('request unexpectedly accepted')

    def run(self, legacy=False):
        self.work = self.root / 'work'; self.work.mkdir()
        (self.work/'supabricks.toml').write_text(f'format_version = 1\nid = "{self.project}"\nname = "extensions"\n')
        self.start()
        self.request(method='resolve_binding', source=dict(definition_id=self.project, worktree=str(self.work)))
        parent = self.create('main')
        before = self.catalogue(parent)
        indexed = {e['name']:e for e in before['extensions']}
        if legacy:
            assert not indexed['pg_stat_statements']['shipped'] and not indexed['pg_stat_statements']['can_enable']
            assert self.sql(parent,'SHOW shared_preload_libraries') == 'neon'
            self.refused(lambda:self.set(parent,'pg_stat_statements',True), 'not shipped')
            self.set(parent,'pg_trgm',True)
            assert self.sql(parent,"SELECT supabricks_extensions.similarity('same','same')=1")=='t'
            self.set(parent,'pg_trgm',False)
            return dict(status='PASS',profile='legacy engine',checks=['missing module reported honestly','no preload/download on old engine','existing bundled module enable and disable'],catalogue=before)
        assert all(indexed[n]['shipped'] and indexed[n]['available_version'] for n in MANAGED), indexed
        assert not indexed['vector']['shipped'] and not indexed['postgis']['shipped']
        assert indexed['plpgsql']['required'] and not indexed['plpgsql']['can_disable']
        self.sql(parent,'CREATE SCHEMA supabricks_extensions AUTHORIZATION supabricks_owner')
        self.refused(lambda:self.set(parent,'pgcrypto',True), 'unsafe ownership')
        self.sql(parent,'DROP SCHEMA supabricks_extensions')
        self.sql(parent,'CREATE EXTENSION pg_trgm SCHEMA public')
        self.refused(lambda:self.set(parent,'pg_trgm',False), 'automatic adoption is refused')
        self.sql(parent,'DROP EXTENSION pg_trgm RESTRICT')
        self.sql(parent,"CREATE FUNCTION public.fixture_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN END $$; CREATE EVENT TRIGGER fixture_ddl ON ddl_command_end EXECUTE FUNCTION public.fixture_ddl()")
        self.refused(lambda:self.set(parent,'pgcrypto',True), 'event triggers are enabled')
        assert not any(e['can_enable'] for e in self.catalogue(parent)['extensions'])
        self.sql(parent,'DROP EVENT TRIGGER fixture_ddl; DROP FUNCTION public.fixture_ddl()')
        for name in MANAGED:
            self.set(parent, name, True)
            self.set(parent, name, True)  # desired-state retry has no duplicate effects
        after = self.catalogue(parent)
        assert all(next(e for e in after['extensions'] if e['name']==n)['enabled'] for n in MANAGED)
        assert self.sql(parent,"SELECT encode(supabricks_extensions.digest('hello','sha256'),'hex')") == '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
        assert self.sql(parent,"SELECT 'Hello'::supabricks_extensions.citext OPERATOR(supabricks_extensions.=) 'hello'::supabricks_extensions.citext, supabricks_extensions.uuid_generate_v4() IS NOT NULL, supabricks_extensions.similarity('word','word')=1") == 't|t|t'
        assert int(self.sql(parent,'SELECT count(*) FROM supabricks_extensions.pg_stat_statements')) > 0
        for name in ('neon', 'plpgsql', 'postgis', 'pgcrypto" CASCADE;--'):
            self.refused(lambda n=name:self.set(parent,n,False), 'not in the managed')
        stale=self.target(parent);stale['revision']-=1
        self.refused(lambda:self.api(dict(action='set',target=stale,name='pgcrypto',enabled=False)), 'Branch changed')
        self.sql(parent,'CREATE TABLE public.citext_data(value supabricks_extensions.citext); INSERT INTO public.citext_data VALUES (\'Hello\')')
        self.refused(lambda:self.set(parent,'citext',False), '2BP01')
        assert self.sql(parent,'SELECT value FROM public.citext_data') == 'Hello'
        # A second physical branch inherits extensions, then diverges safely.
        created=self.request(method='api',api_version=1,binding=dict(project_id=self.project,worktree=str(self.work)),action=dict(action='create_branch',parent='main',name='child',key='extension-child'))
        self.operation(created)
        child=self.request(method='branch',id=created['branch_id'])
        wait(lambda:self.sql(child,'SELECT 1')=='1')
        assert next(e for e in self.catalogue(child)['extensions'] if e['name']=='pg_trgm')['enabled']
        self.set(child,'pg_trgm',False)
        assert next(e for e in self.catalogue(parent)['extensions'] if e['name']=='pg_trgm')['enabled']
        parent=self.state(parent,'suspended')
        self.refused(lambda:self.catalogue(parent), 'running, ready')
        parent=self.state(parent,'running');wait(lambda:self.sql(parent,'SELECT 1')=='1')
        assert next(e for e in self.catalogue(parent)['extensions'] if e['name']=='citext')['enabled']
        self.stop(); self.start();wait(lambda:self.sql(parent,'SELECT 1')=='1')
        assert self.sql(parent,'SELECT value FROM public.citext_data')=='Hello'
        self.sql(parent,'DROP TABLE public.citext_data')
        for name in MANAGED:
            self.set(parent,name,False);self.set(parent,name,False)
        assert not any(e['enabled'] for e in self.catalogue(parent)['extensions'] if e['name'] in MANAGED)
        self.stop();self.start();wait(lambda:self.sql(parent,'SELECT 1')=='1')
        assert not any(e['enabled'] for e in self.catalogue(parent)['extensions'] if e['name'] in MANAGED)
        # The daemon checks branch project ownership before opening privileged SQL.
        foreign=self.root/'foreign';foreign.mkdir(); foreign_id=str(uuid.uuid4())
        (foreign/'supabricks.toml').write_text(f'format_version = 1\nid = "{foreign_id}"\nname = "foreign"\n')
        self.request(method='resolve_binding',source=dict(definition_id=foreign_id,worktree=str(foreign)))
        self.refused(lambda:self.api(dict(action='list',target=self.target(parent)),dict(project_id=foreign_id,worktree=str(foreign))), 'not found')
        result=subprocess.run([str(self.binary),'extensions','list','--branch','main','--project',str(self.work),'--data-dir',str(self.root)],capture_output=True,text=True,env=self.env,timeout=20)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['branch_id']==parent['branch']['id']
        self.checks.extend(['unsafe schema refused','unmanaged extension adoption refused','event trigger fence preserved','all five shipped and callable','required and unknown refused','stale revision refused','dependent table preserved','physical branch inheritance and isolation','suspended compute not awakened','suspend/resume and daemon restart','cross-project denied','CLI catalogue','desired-state retry','disable persists across restart'])
        return dict(status='PASS',checks=self.checks,before=before,enabled=after)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('binary','bundle','helpers','report'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--legacy',action='store_true')
    args=p.parse_args();os.umask(0o077)
    root=Path(tempfile.mkdtemp(prefix='pg253-',dir='/tmp')).resolve()
    cell=Extensions(args.binary.resolve(),args.bundle.resolve(),args.helpers.resolve(),root)
    try:
        result=cell.run(args.legacy);args.report.write_text(json.dumps(result,indent=2)+'\n');print('PG253 PASS')
    except BaseException:
        print('Failed fixture retained at',root,flush=True);raise
    finally:
        if (root/'control.sock').exists():cell.stop()

if __name__=='__main__':main()
