"""UC02 project-owned metadata against actual bundled UC and PostgreSQL."""
import json
import subprocess
import socket
import uuid
from cell import wait


def run(cell, root, installed, branch, work, endpoint, token, api, check):
    binding=dict(project_id=cell.project,worktree=str(work))
    def submit(command, scope=None):
        return cell.request(method='api',api_version=1,binding=scope or binding,action=dict(action='catalog_metadata',command=command))
    def metadata(action, scope=None, expect='complete', **fields):
        value=submit(dict(action=action,**fields),scope)
        if value.get('state')=='running':
            request=value['id']
            value=wait(lambda:(v if (v:=submit(dict(action='poll',id=request),scope))['state']!='running' else None),timeout=55)
            assert value['state']==expect,value
            return value['result'] if expect=='complete' else value['error']
        return value
    assert metadata('namespace')['namespace'] is None
    assert metadata('list',branch='main')['assets']==[]
    assert metadata('health')['catalog']['ready']
    namespace=metadata('ensure_namespace')['namespace']
    assert namespace['state']=='ready' and namespace['catalog_id'] and namespace['schema_id']
    assert metadata('namespace')['namespace']==namespace
    assert token() not in json.dumps(namespace)
    check('uc02_owned_namespace_creation_and_stable_uuid_resolution')
    def ddl(sql):
        # Product metadata runs as the application owner, not the engine admin.
        return cell.sql(branch,'SET ROLE supabricks_owner; '+sql)
    definition=str(uuid.uuid4());scopes=[];namespaces=[]
    for name in ('deployment-a','deployment-b'):
        path=root/name;path.mkdir()
        (path/'supabricks.toml').write_text(f'''format_version=2
id="{definition}"
name="uc02"
[package]
version="0.1.0"
include=[]
notebook_outputs="strip"
[targets.local]
mode="development"
default=true
''')
        context=cell.request(method='project',source=dict(definition_id=definition,worktree=str(path)),command=dict(action='create',key=name,target=None))
        scope=dict(project_id=context['runtime_project_id'],worktree=str(path));scopes.append(scope)
        namespaces.append(metadata('ensure_namespace',scope=scope)['namespace'])
    assert namespaces[0]['catalog']!=namespaces[1]['catalog'] and namespaces[0]['catalog_id']!=namespaces[1]['catalog_id']
    assert namespaces[0]['project_id']!=namespaces[1]['project_id']
    check('uc02_same_definition_deployments_have_distinct_owned_namespaces')
    ddl('CREATE TABLE uc02_items(id integer NOT NULL); INSERT INTO uc02_items VALUES(42); CREATE TABLE "UC02.Quoted"("OddName" text)')
    first=metadata('list',branch='main')
    live=next(a for a in first['assets'] if a['name']=='uc02_items')
    quoted=next(a for a in first['assets'] if a['name']=='UC02.Quoted')
    assert live['provider']=='postgres' and live['uc_object_id'] is None
    assert quoted['alias']=='"public"."UC02.Quoted"'
    assert metadata('resolve',id=live['id'],expected_version=live['version'])['storage_access'] is False
    try:metadata('describe',scope=scopes[0],id=live['id'])
    except RuntimeError:pass
    else:raise AssertionError('foreign deployment described a source asset')
    check('uc02_live_metadata_quotes_identifiers_and_fences_project_ownership')
    page=metadata('list',branch='main',limit=1);assert page['next']
    assert metadata('list',branch='main',limit=1,after=page['next'])['assets']
    ddl('ALTER TABLE uc02_items ADD COLUMN label text')
    assert metadata('validate_source',id=live['id'],expected_version=live['version'],expect='failed')['code']=='schema_drift'
    assert metadata('list',branch='main',after=page['next'],expect='failed')['code']=='stale_version'
    current=metadata('describe',id=live['id'])['asset'];assert current['id']==live['id'] and current['version']!=live['version']
    ddl('ALTER TABLE uc02_items RENAME TO uc02_renamed')
    assert metadata('resolve',id=current['id'],expected_version=current['version'],expect='failed')['code']=='stale_version'
    renamed=metadata('describe',id=current['id'])['asset'];assert renamed['name']=='uc02_renamed'
    ddl('DROP TABLE uc02_renamed; CREATE TABLE uc02_renamed(id integer)')
    assert metadata('resolve',id=renamed['id'],expected_version=renamed['version'],expect='failed')['code']=='identity_changed'
    replacement=next(a for a in metadata('list',branch='main')['assets'] if a['name']=='uc02_renamed');assert replacement['id']!=renamed['id']
    check('uc02_schema_drift_rename_recreation_and_stale_pagination_fail_closed')
    ddl('CREATE TABLE "UC02_RENAMED"(id integer)')
    assert metadata('validate_source',id=replacement['id'],expected_version=replacement['version'],expect='failed')['code']=='quoting_collision'
    ddl('DROP TABLE "UC02_RENAMED"')
    check('uc02_case_collisions_rejected_before_publication')
    cell.work=work;cell.python=installed/'python/analytics/python'
    _,publication=cell.publish()
    assets=metadata('list',branch='main')['assets'];delta=next(a for a in assets if a['name']=='uc02_renamed' and a['kind']=='delta_snapshot')
    assert delta['id']!=replacement['id'] and delta['epoch_id']==publication['epoch_id']
    assert delta['provider']=='supabricks_snapshot' and delta['publication_revision']==publication['ordinal'] and delta['snapshot_at_ms']
    assert 'generation' not in json.dumps(delta) and 'password' not in json.dumps(delta)
    assert metadata('resolve',id=delta['id'],expected_version=delta['version'])['asset']['id']==delta['id']
    check('uc02_unified_live_and_published_metadata_preserves_provenance')
    # Recreating a UC schema under the same alias must invalidate the stored UUID.
    n=namespaces[0];path='schemas/'+n['catalog']+'.'+n['schema']
    assert api(endpoint,token(),path,method='DELETE')[0] in (200,204)
    assert api(endpoint,token(),'schemas',method='POST',body=dict(catalog_name=n['catalog'],name=n['schema']))[0] in (200,201)
    assert metadata('namespace',scope=scopes[0],expect='failed')['code']=='identity_changed'
    check('uc02_recreated_uc_schema_cannot_replace_recorded_ownership')
    output=subprocess.check_output([str(cell.binary),'catalog','metadata','health','--project',str(work),'--data-dir',str(cell.root)],text=True)
    assert json.loads(output)['api_version']==1
    for command in (['catalog','metadata','ensure-namespace'],['mcp']):
        result=subprocess.run([str(cell.binary),*command,'--data-dir',str(cell.root)],cwd=root,capture_output=True,text=True)
        assert result.returncode!=0
    check('uc02_cli_contract_and_projectless_operations_rejected')

    with socket.socket() as blocked:
        blocked.bind(('127.0.0.1',0))
        expected=metadata('health')['catalog']['metastore_id']
        cell.request(method='catalog_service',command=dict(action='configure',provider=dict(mode='external',endpoint=f'http://127.0.0.1:{blocked.getsockname()[1]}',token_file=str(cell.root/'catalog/etc/conf/token.txt'),ca_file=None,metastore_id=expected)))
        wait(lambda:metadata('health')['catalog']['state']=='unavailable')
        assert metadata('list',branch='main')['assets']
        assert cell.sql(branch,'SELECT 42')=='42'
        cell.request(method='catalog_service',command=dict(action='configure',provider=dict(mode='local',runtime=None)))
        wait(lambda:metadata('health')['catalog']['ready'])
    check('uc02_unavailable_catalog_is_distinct_from_empty_and_pg_metadata_remains_usable')
