#!/usr/bin/env python3
"""Fail closed if IAM00 evidence is incomplete, stale or claims product governance."""
import argparse
import json
from pathlib import Path
from common import HERE, PINS, digest

IDENTITY_CHECKS = {
    'signed_oidc_subjects_and_equal_emails_are_distinct','stable_mapping_survives_reopen',
    'signed_external_admin_label_positive_control','external_admin_label_has_no_broker_elevation',
    'broker_admin_label_is_ordinary_principal','broker_admin_label_cannot_create_catalog','broker_rejects_wrong_audience','uc_exchange_rejects_wrong_audience',
    'broker_rejects_forged_issuer','uc_exchange_rejects_forged_issuer',
    'native_exchange_alice','native_exchange_same-email','native_external_admin_label_exchange_observed',
    'native_group_endpoint_characterized','email_change_preserves_principal',
    'recreated_subject_does_not_inherit_identity','recreated_subject_does_not_inherit_grants',
    'introspection_active_positive_control','idp_disable_detected_before_jwt_expiry',
    'disabled_identity_admission_denied','recreated_identity_admission_positive_control',
    'idp_outage_fences_admission','idp_recovery_requires_active_identity','forged_signature_rejected',
}
PG_DENIALS = {'other_table','control_role','owner_role','server_file','server_program','superuser',
             'create_role','grant_read_all','replication','bypass_rls','other_branch','control_login','owner_login'}
SANDBOX_CHECKS = {'native_numpy_pandas_arrow','python_user_is_unprivileged','own_admitted_data_readable',
    'filesystem_denied:/host-secrets/credential','filesystem_denied:/host-secrets/control.sock',
    'filesystem_denied:/host-secrets/other/private-cache','filesystem_denied:/host-secrets/other/private-credential',
    'filesystem_denied:/other-data/owner','filesystem_denied:/other-data/data100/_delta_log/00000000000000000000.json',
    'filesystem_denied:/work/config.json','filesystem_denied:/proc/1/root/host-secrets/credential',
    'unix_socket_denied:/var/run/docker.sock','unix_socket_denied:/host-secrets/control.sock',
    'unix_socket_denied:/run/runsc/alice_control.sock','no_host_secret_environment','no_other_principal_processes',
    'cannot_elevate_to_root','raw_network_socket_denied',
    'product_mount_is_readonly','scratch_quota_is_512m','scratch_over_quota_denied',
    'sail_raw_unadmitted_delta_denied','sail_delta_10mb_full_payload','native_deltalake_10mb',
    'sail_delta_100mb_full_payload','native_deltalake_100mb','bounded_execution_revocation','outer_cgroup_limits'}


def validate(report, check_sources=True):
    assert report['schema_version']==1
    assert report['status']=='PASS_WITH_LIMITATIONS'
    assert report['profile']=='iam00-feasibility-only' and report['governed_product_enabled'] is False
    assert report['pins']==PINS,'component pins differ'
    assert report['release_inventory_sha256']==PINS['platform']['inventory_sha256']
    assert report['platform_revision']==PINS['platform']['commit']
    assert report['phases']==dict(postgres='PASS',identity='PASS',sandbox='PASS')
    assert report['cleanup']==[dict(name=n,status='PASS') for n in ('catalog','keycloak','cell')]
    checks=report['evidence']['checks']
    names={c['name'] for c in checks}
    assert len(names)==len(checks) and all(c['status']=='PASS' for c in checks)
    required=IDENTITY_CHECKS|{'pg_restricted_positive_control','pg_running_query_terminated',
        'two_concurrent_managed_notebooks_and_sail_instances','both_sandbox_controllers_exit_successfully',
        'owned_sandbox_containers_removed','owned_sandbox_host_processes_reaped'}
    required|={'pg_denies_'+n for n in PG_DENIALS}
    required|={'uc_table_access_'+n for n in ('alice','bob','same-email','service')}
    required|={'uc_metadata_filter_'+n for n in ('bob','same-email','service')}
    required|={'sail_uc_table_read_'+n for n in ('alice','bob','service')}
    for who in ('alice','bob'): required|={who+':'+n for n in SANDBOX_CHECKS}
    assert required<=names, 'missing required checks: '+str(sorted(required-names))
    expected_limits={'native_uc_email_identity_collision','native_external_admin_label_not_safe_for_direct_federation',
                     'native_groups_unavailable_use_materialized_direct_grants'}
    limits={item['name']:item for item in report['evidence']['limitations']}
    assert set(limits)==expected_limits,'reassess architectural decision when capability changes'
    assert limits['native_uc_email_identity_collision']['same_uc_subject'] is True
    assert limits['native_external_admin_label_not_safe_for_direct_federation']['create_catalog_http']==200
    metrics=report['evidence']['metrics']
    assert 0 <= metrics['idp_disable_seconds'] < 300
    assert 0 <= metrics['pg_termination_seconds'] < 60
    for who,mode in [('alice','revoke'),('bob','daemon-loss')]:
        value=metrics[who]
        assert value['uid']==1000 and value['scratch_capacity_bytes']==512*1024*1024
        assert value['outer_cgroup_memory_max']=='2147483648'
        assert value['outer_cgroup_pids_max']=='512' and value['outer_cgroup_cpu_max']=='200000 100000'
        assert 0 < value['outer_memory_peak_bytes'] <= 2147483648
        assert 0 < value['notebook_startup_seconds'] < 180
        assert value['kernel_busy_before_revoke'] and value['running_query_cpu_seconds']>=.15
        revocation=value['revocation']
        assert revocation['mode']==mode and 0<=revocation['seconds']<60
        assert revocation['heartbeat_stopped'] and revocation['runtime_empty'] and revocation['open_descriptor_activity_before']
        assert revocation['issuer_killed']==(mode=='daemon-loss')
        assert revocation['lease_seconds']==2
        for size in (10,100): assert 0 < value['metrics'][f'delta_{size}mb_read_seconds'] < 120
        assert len([n for n in names if n.startswith(who+':tcp_denied:127.0.0.1:')])==5
    if check_sources:
        repo=HERE.parents[2]
        expected={str(p.relative_to(repo)):digest(p) for p in HERE.glob('*') if p.is_file()}
        assert report['source_sha256']==expected,'probe sources changed since this evidence'
    return len(checks)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    print(f'IAM00 qualified: {validate(json.loads(args.report.read_text()))} checks; governed product remains disabled')
