"""UC09.8 acceptance: governed claims require the same archive as every R04 gate."""
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
PROFILE = 'linux-governed-shared-v1'
TOP_CHECKS = {'signed_curl_install_and_verified_unchanged_candidate',
              'installed_payload_readable_by_distinct_guest_uid',
              'installed_private_runtime_preparation_without_downloads',
              'archive_and_installed_inventory_unchanged_after_all_suites',
              *('exact_installed_'+name for name in ('identity','data','upgrade','browser'))}
REQUIRED = {
    'identity': {
        'real TLS Keycloak PKCE logins and equal-email principal separation',
        'two real users enforce conflicting roles, explicit execution, idempotency and stop ownership',
        'service execution approval binds the exact immutable source and rejects edited code',
        'real Keycloak users use distinct private UC mappings through the authenticated CLI and daemon; UC returns no ungranted metadata',
        'real OIDC, CLI, daemon, project admission and UC broker launch isolated Jupyter/Sail SQL; another actor cannot read results',
        'IdP disable blocks execution renewal and the independent watchdog removes the sandbox within 35 seconds',
        'IdP disable refuses an unexpired platform session',
        'provider outage fails closed while local logout remains available',
        'scoped service credential and session rotation',
        'audited identity transitions without provider credentials',
        'disposable provider, login clients and daemon cleaned up',
    },
    'data': {
        'project_admin_does_not_imply_pg_read', 'restricted_pg_positive_control_and_actor_private_results',
        'read_denies_control_files_owner_and_write', 'separate_pg_write_and_ddl',
        'single_statement_transaction_and_role_cleanup', 'rls_source_is_not_whole_branch',
        'revocation_rolls_back_write_and_timeouts_close_sessions',
        'sbdata_copy_requires_source_and_destination_authority_and_is_atomic',
        'destination_grant_race_rolls_back_import',
        'clone_rotates_source_credentials_and_does_not_inherit_grants',
        'frozen_export_publication_requires_fresh_copy_and_sharing_grants',
        'grant_race_cleans_export_without_replacing_active_revision',
        'writer_crash_rolls_back_and_never_replays_sql',
        'governed_backup_restore_closes_historical_grants_and_rotates_pg_and_sessions',
        'bounded_audit_export_contains_correlated_metadata_without_credentials_or_sql',
        'audit_append_failure_rolls_back_identity_mutation',
        'audit_capacity_closes_admission_across_restart_and_remains_exportable',
    },
    'upgrade': {
        'qualified_predecessor_native_pg_and_published_uc_snapshot',
        'named_uc_transition_uses_source_binary_checkpoint_and_preserves_backup',
        'upgraded_pg_publication_and_metastore_ids_survive_with_rotated_uc_credentials',
        'pre_upgrade_backup_restores_with_its_original_catalog_binary',
    },
    'browser': {
        'independent_oidc_browser_sessions', 'browser_project_creation_and_denied_discovery',
        'browser_governed_package_ingestion', 'browser_reviewed_snapshot_publication',
        'reviewed_uc_sharing_and_filtered_discovery', 'browser_bound_spark_query',
        'source_pinned_service_use_from_browser', 'revocation_during_bound_notebook_execution',
        'crafted_administration_denied', 'correlated_secret_free_audit',
        'revoked_session_clears_browser_data', 'concurrent_execution_limit_and_measured_cgroups',
        'sandbox_bypass_and_scratch_quota_denied', 'revocation_preserves_independent_peer_execution',
    },
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:raise ValueError('governed: '+message)


def sha(value):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value) is not None


def collect(data, env):
    identity=env['release_sha256']
    require(data.get('schema_version')==1 and data.get('status')=='passed' and data.get('profile')==PROFILE,
            'missing complete governed qualification')
    require(data.get('target')=='linux-x86_64' and data.get('release_identity')==identity,'mixed archive identity/target')
    require(data.get('archive')==env['archive'] and data.get('source')==env['source'],'mixed source or archive')
    require(not any(data.get(k) for k in ('error','errors','failure','cleanup_errors')),'failed qualification')
    require(data.get('cleanup')==dict(leaked_containers=0,remaining_containers=0),'leaked containers')
    require(TOP_CHECKS<=set(data.get('checks',[])),'incomplete installer/runtime qualification')
    host=data.get('host_capacity',{})
    require(host.get('cpu_count')==4 and type(host.get('memory_bytes')) is int
            and 14*1024**3<=host['memory_bytes']<=16*1024**3,
            'qualification requires the dedicated 4-core/16-GiB host envelope')
    require(sha(data.get('binary_sha256')) and sha(data.get('console_manifest_sha256')),'missing executable/asset hashes')
    require(data['console_manifest_sha256']==env['source']['console']['manifest_sha256'],'different console assets')
    require(re.fullmatch('sha256:[a-f0-9]{64}',data.get('qualifier_image','')) is not None,'unidentified qualifier image')
    network=data.get('network',{})
    require(network.get('mode')=='none' and network.get('external_attempts')==2 and network.get('external_denials')==2,
            'external network not denied')
    pins=json.loads((ROOT/'components/execution-runtime.lock.json').read_text())
    idp=json.loads((ROOT/'e2e/native/iam/pins.lock.json').read_text())['keycloak']['image']
    components=data.get('components',{})
    require(components.get('execution_release_identity')==identity,'workload uses a different archive')
    require(components.get('keycloak')==idp and components.get('rootfs_image')==pins['rootfs']
            and components.get('gvisor_inventory_sha256')==pins['gvisor']['inventory_sha256'],'unreviewed isolation/IdP inputs')
    for key,path in [('execution_pin_sha256','components/execution-runtime.lock.json'),
                     ('installer_template_sha256','install/native/install.sh.in'),
                     ('supervisor_sha256','python/execution/supervisor.py'),('workload_sha256','python/execution/workload.py')]:
        require(components.get(key)==digest(ROOT/path),'changed '+key)
    require(all(sha(components.get(k)) for k in ('rootfs_sha256','execution_config_sha256')),'missing runtime configuration hashes')
    suites=data.get('suites',{})
    require(set(suites)==set(REQUIRED),'missing governed suite')
    reports={}
    for name,required in REQUIRED.items():
        suite=suites[name]
        checks=suite.get('checks',[])
        require(suite.get('status') in ('PASS','passed') and suite.get('exit_code')==0,'failed '+name)
        require(isinstance(checks,list) and all(isinstance(c,str) for c in checks)
                and len(checks)==len(set(checks)) and required<=set(checks),'incomplete '+name)
        require(suite.get('release_identity')==identity and suite.get('binary_sha256')==data['binary_sha256'],'mixed '+name+' runtime')
        cleanup=suite.get('cleanup',{})
        require(cleanup.get('exit_code')==0 and cleanup.get('timed_out') is False
                and cleanup.get('leaked_descendants')==0 and cleanup.get('remaining_descendants')==0
                and cleanup.get('descendants_observed',0)>0,'incomplete '+name+' process cleanup')
        require(sha(suite.get('report_sha256')),'missing '+name+' report hash')
        reports[name]=dict(sha256=suite['report_sha256'],checks=sorted(required))
    require(suites['upgrade'].get('predecessor_identity')==pins['platform']['inventory_sha256'],'unqualified predecessor')
    browser=suites['browser']
    require(browser.get('tls') is True and browser.get('exact_installed') is True,'browser did not use installed TLS ingress')
    require(browser.get('console_manifest_sha256')==data['console_manifest_sha256'],'browser asset mismatch')
    latency=browser.get('revocation_observed_ms')
    require(type(latency) in (int,float) and math.isfinite(latency) and 0<=latency<=60000,'execution revocation missed its bound')
    require(browser.get('resource_envelope')==dict(concurrent_executions=2,memory_bytes=2147483648,cpu_count=2,pids=512,scratch_bytes=536870912),
            'unqualified execution resource envelope')
    memory=browser.get('execution_memory',[])
    require(len(memory)==2 and all(type(m.get('current_bytes')) is int and type(m.get('peak_bytes')) is int
            and 0<m['current_bytes']<=m['peak_bytes']<=2147483648 for m in memory),
            'missing concurrent cgroup memory observations')
    disable=suites['identity'].get('measurements',{}).get('idp_disable',{})
    interval=[disable.get(k) for k in ('acknowledged_deny_monotonic','observed_closed_monotonic')]
    require(all(type(v) in (int,float) and math.isfinite(v) for v in interval)
            and 0<=interval[1]-interval[0]<300,'IdP disable missed its bound')
    return dict(status='passed',profile=PROFILE,release_identity=identity,reports=reports,
                components={k:components[k] for k in ('keycloak','rootfs_image','rootfs_sha256','gvisor_inventory_sha256',
                    'execution_config_sha256','execution_release_identity','execution_pin_sha256','installer_template_sha256','supervisor_sha256','workload_sha256')},
                revocation_observed_ms=latency,idp_disable_seconds=interval[1]-interval[0],resource_envelope=browser['resource_envelope'],
                host_capacity=dict(cpu_count=4,memory_bytes=host['memory_bytes']),
                execution_memory=[{k:m[k] for k in ('current_bytes','peak_bytes')} for m in memory])
