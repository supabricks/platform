-- Durable service authority is independent of browser and bearer sessions.
ALTER TABLE identity_principals ADD COLUMN sync_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE data_grants RENAME TO data_grants_v27;
CREATE TABLE data_grants (
 deployment TEXT NOT NULL REFERENCES deployments(id),
 branch TEXT NOT NULL REFERENCES branches(id), subject TEXT NOT NULL,
 capability TEXT NOT NULL CHECK(capability IN ('read','write','ddl','copy_source','receive','share','manage_sync','execute_sync','read_sync')),
 PRIMARY KEY(deployment,branch,subject,capability)
);
INSERT INTO data_grants SELECT * FROM data_grants_v27;
DROP TABLE data_grants_v27;

-- Invalidate cached UC/execution decisions when a producer service loses authority.
CREATE TRIGGER sync_service_revoke AFTER UPDATE OF sync_generation ON identity_principals
WHEN OLD.sync_generation!=NEW.sync_generation AND EXISTS (
 SELECT 1 FROM sync_policies WHERE json_extract(record,'$.service_authority.principal_id')=NEW.id
) BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;
CREATE TRIGGER sync_source_policy_revoke AFTER UPDATE OF revision ON authorization_policy
WHEN OLD.revision!=NEW.revision AND EXISTS (
 SELECT 1 FROM sync_policies WHERE json_extract(record,'$.deployment_id')=NEW.deployment
 AND json_extract(record,'$.service_authority') IS NOT NULL
) BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;

CREATE TRIGGER sync_run_admission_audit AFTER INSERT ON sync_runs
WHEN EXISTS (SELECT 1 FROM sync_policies WHERE id=NEW.policy_id AND json_extract(record,'$.service_authority') IS NOT NULL)
BEGIN
 INSERT INTO security_audit(at_ms,event) SELECT json_extract(NEW.record,'$.admitted_at_ms'),
 json_object('stream','sync','action','run.admitted','policy_id',NEW.policy_id,'run_id',NEW.id,
 'branch_id',NEW.branch_id,'deployment_id',json_extract(record,'$.deployment_id'),
 'effective_principal_id',json_extract(record,'$.service_authority.principal_id'),
 'policy_revision',json_extract(record,'$.revision'),'state',NEW.state)
 FROM sync_policies WHERE id=NEW.policy_id;
END;
CREATE TRIGGER sync_run_transition_audit AFTER UPDATE OF state ON sync_runs
WHEN OLD.state!=NEW.state AND EXISTS (SELECT 1 FROM sync_policies WHERE id=NEW.policy_id AND json_extract(record,'$.service_authority') IS NOT NULL)
BEGIN
 INSERT INTO security_audit(at_ms,event) SELECT CAST(strftime('%s','now') AS INTEGER)*1000,
 json_object('stream','sync','action','run.transition','policy_id',NEW.policy_id,'run_id',NEW.id,
 'branch_id',NEW.branch_id,'deployment_id',json_extract(record,'$.deployment_id'),
 'effective_principal_id',json_extract(record,'$.service_authority.principal_id'),
 'state',NEW.state,'epoch_id',json_extract(NEW.record,'$.epoch_id'),'source_lsn',json_extract(NEW.record,'$.source_lsn'))
 FROM sync_policies WHERE id=NEW.policy_id;
END;
