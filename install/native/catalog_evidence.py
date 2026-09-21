"""UC08 gate: bind catalog coverage and costs to the inherited exact archive."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = {
    'service': {
        'installed_private_jre_authenticated_loopback_bootstrap',
        'uc03_preview_idempotent_publish_crash_recovery_and_complete_alias',
        'uc03_refresh_retains_old_revision_across_gc_and_stopped_daemon',
        'uc04_console_sql_spark_connect_notebook_share_frozen_revision_and_aliases',
        'daemon_restart_preserves_catalog_metadata',
        'uc05_offline_pack_verify_import_preserve_requirements_without_destination_authority',
        'uc05_two_projects_share_one_publication_without_copies_or_ownership_transfer',
    },
    'browser': {
        'producer_created_ingested_and_published_in_browser',
        'bound_dataset_queried_in_spark_workspace',
        'live_schema_drift_requires_explicit_reinspection',
        'notebook_handoff_uses_current_fixed_binding_without_automatic_execution',
    },
    'recovery': {
        'two_project_bound_read_before_checkpoint',
        'interrupted_restore_remains_guarded_and_original_backup_verifies',
        'moved_root_preserves_ids_and_bindings_relocates_files_and_rotates_credentials',
        'restored_binding_keeps_snapshot_retained',
        'exact_archive_unchanged_after_catalog_restore_and_rebind',
    },
}
MINIMUM = dict(service=37, browser=11, recovery=9)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def expected_build(target):
    pin=json.loads((ROOT/'components/unity-catalog-source.lock.json').read_text())
    return dict(schema_version=2,target=target,source_commit=pin['commit'],source_dirty=False,
        source_pin_sha256=digest(ROOT/'components/unity-catalog-source.lock.json'),
        dependency_lock_sha256=digest(ROOT/'components/unity-catalog-maven.lock.json'),
        builder_script_sha256=digest(ROOT/'components/build-unity-catalog.py'),
        java=pin['java'],sbt=pin['sbt'],source_inputs=pin['inputs'])


def collect(data, env):
    # Imported lazily because R04 owns generic evidence validation.
    from release_evidence import require, sha, metrics, passed
    target=env['archive']['target']
    passed(data,5,'catalog')
    require(not data.get('failure') and not data.get('active_gate'),'catalog: incomplete gate')
    require(data.get('release_identity')==env['release_sha256'] and data.get('archive')==env['archive'],
            'catalog: mixed release or archive identity')
    require(data.get('source')==env['source'],'catalog: mixed component provenance')
    require(data.get('demo',{}).get('CATALOG-DEMO.md')==digest(ROOT/'docs/handbook/catalog-demo.md'),
            'catalog: missing or different installed catalog walkthrough')
    require(bool(data.get('network_evidence')),'catalog: network evidence missing')
    build=data.get('unity_catalog',{})
    require(all(build.get(k)==v for k,v in expected_build(target).items()),'catalog: unreviewed UC/JRE build')
    pin=data['source'].get('unity_catalog',{})
    require(sha(data.get('uc_build_sha256')) and data['uc_build_sha256']==pin.get('build_sha256'), 'catalog: build provenance differs')
    require(pin.get('source_commit')==build['source_commit'] and pin.get('metadata_backend')=='h2-2.2.224'
            and pin.get('profile')=='local-owner-files','catalog: unsupported capability profile')
    contract=data.get('contract',{})
    require(contract.get('backend_schema')==1 and contract.get('publication_manifest')==1
            and contract.get('server_commit')==build['source_commit']
            and contract.get('capability_profile')=='local-owner-files-v1','catalog: unsupported metadata contract')
    suites=data.get('suites',{})
    require(set(suites)==set(REQUIRED),'catalog: missing native, browser or recovery suite')
    for name,required in REQUIRED.items():
        suite=suites[name]; checks=suite.get('checks',[])
        require(suite.get('status')=='PASS' and suite.get('exit_code')==0,'catalog: failed '+name)
        require(len(set(checks))>=MINIMUM[name] and required<=set(checks),'catalog: incomplete '+name)
        cleanup=suite.get('cleanup') or {}
        require(cleanup.get('exit_code')==0 and cleanup.get('timed_out') is False
                and cleanup.get('leaked_descendants')==0 and cleanup.get('remaining_descendants')==0
                and cleanup.get('descendants_observed',0)>0,'catalog: incomplete descendant cleanup')
        require(sha(suite.get('report_sha256')),'catalog: missing report hash')
    if target=='linux-x86_64':
        require('bounded_filesystem_enospc_publishes_no_backup_and_preserves_source' in suites['recovery']['checks'],
                'catalog: Linux ENOSPC not qualified')
    measurements=metrics(data.get('measurements',{}))
    for name in ('peak_rss_bytes','catalog_peak_rss_bytes','catalog_processes_observed','duration_seconds'):
        require(measurements.get(name,0)>0,'catalog: missing resource measurement '+name)
    return dict(commit=build['source_commit'],java=build['java'],contract=contract,
                build_sha256=data['uc_build_sha256'],measurements=measurements,
                suites=suites,network=data['network_evidence'])
