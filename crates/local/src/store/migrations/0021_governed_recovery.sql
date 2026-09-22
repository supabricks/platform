-- UC09.6: short authoritative renewals and bounded, exportable security audit.
ALTER TABLE identity_sessions ADD COLUMN authoritative_until_ms INTEGER NOT NULL DEFAULT 0;
CREATE TABLE security_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), clock_ms INTEGER NOT NULL DEFAULT 0,
 restore_closed INTEGER NOT NULL DEFAULT 0 CHECK(restore_closed IN (0,1)),
 recovery_pending INTEGER NOT NULL DEFAULT 0, restore_id TEXT, source_realm TEXT
);
INSERT INTO security_state(singleton) VALUES (1);
CREATE TABLE security_audit (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
 event TEXT NOT NULL CHECK(json_valid(event) AND length(CAST(event AS BLOB))<=8192)
);
CREATE TRIGGER security_audit_capacity BEFORE INSERT ON security_audit BEGIN
 SELECT CASE WHEN (SELECT count(*) FROM security_audit)>=10000
 THEN RAISE(ABORT,'security audit full; export and acknowledge retained events') END;
END;
INSERT INTO security_audit(at_ms,event) SELECT at_ms,json_object('stream','identity','actor_id',actor,'effective_principal_id',effective_principal,'action',action,'target',target) FROM identity_audit;
CREATE TRIGGER security_identity_audit AFTER INSERT ON identity_audit BEGIN
 INSERT INTO security_audit(at_ms,event) VALUES(NEW.at_ms,json_object('stream','identity','actor_id',NEW.actor,'effective_principal_id',NEW.effective_principal,'action',NEW.action,'target',NEW.target));
 DELETE FROM identity_audit WHERE sequence <= NEW.sequence-1000;
END;
DELETE FROM identity_audit WHERE sequence <= (SELECT coalesce(max(sequence),0)-1000 FROM identity_audit);
INSERT INTO security_audit(at_ms,event) SELECT at_ms,json_object('stream','authorization','realm_id',realm,'deployment_id',deployment,'actor_id',actor,'effective_principal_id',effective_principal,'policy_revision',policy_revision,'action',action,'target',target,'source_revision',(SELECT source_revision FROM authorization_executions WHERE id=target),'datasets',json((SELECT datasets_json FROM isolated_executions WHERE id=target)),'branch_id',(SELECT branch FROM data_operations WHERE id=target),'branch_revision',(SELECT branch_revision FROM data_operations WHERE id=target),'request_sha256',(SELECT request_hash FROM data_operations WHERE id=target),'data_revision',json((SELECT json_extract(result_json,'$.data_revision') FROM data_operations WHERE id=target)),'export_id',(SELECT json_extract(result_json,'$.export_id') FROM data_operations WHERE id=target),'outcome',coalesce((SELECT state FROM data_operations WHERE id=target),(SELECT state FROM isolated_executions WHERE id=target),'recorded')) FROM authorization_audit;
CREATE TRIGGER security_authorization_audit AFTER INSERT ON authorization_audit BEGIN
 INSERT INTO security_audit(at_ms,event) VALUES(NEW.at_ms,json_object('stream','authorization','realm_id',NEW.realm,'deployment_id',NEW.deployment,'actor_id',NEW.actor,'effective_principal_id',NEW.effective_principal,'policy_revision',NEW.policy_revision,'action',NEW.action,'target',NEW.target,'source_revision',(SELECT source_revision FROM authorization_executions WHERE id=NEW.target),'datasets',json((SELECT datasets_json FROM isolated_executions WHERE id=NEW.target)),'branch_id',(SELECT branch FROM data_operations WHERE id=NEW.target),'branch_revision',(SELECT branch_revision FROM data_operations WHERE id=NEW.target),'request_sha256',(SELECT request_hash FROM data_operations WHERE id=NEW.target),'data_revision',json((SELECT json_extract(result_json,'$.data_revision') FROM data_operations WHERE id=NEW.target)),'export_id',(SELECT json_extract(result_json,'$.export_id') FROM data_operations WHERE id=NEW.target),'outcome',coalesce((SELECT state FROM data_operations WHERE id=NEW.target),(SELECT state FROM isolated_executions WHERE id=NEW.target),'recorded')));
 DELETE FROM authorization_audit WHERE sequence <= NEW.sequence-1000;
END;
DELETE FROM authorization_audit WHERE sequence <= (SELECT coalesce(max(sequence),0)-1000 FROM authorization_audit);
INSERT INTO security_audit(at_ms,event) SELECT at_ms,json_object('stream','catalog','actor_id',actor,'effective_principal_id',actor,'action',action,'target',target,'catalog_revision',revision) FROM catalog_grant_audit;
CREATE TRIGGER security_catalog_grant_audit AFTER INSERT ON catalog_grant_audit BEGIN
 INSERT INTO security_audit(at_ms,event) VALUES(NEW.at_ms,json_object('stream','catalog','actor_id',NEW.actor,'effective_principal_id',NEW.actor,'action',NEW.action,'target',NEW.target,'catalog_revision',NEW.revision));
 DELETE FROM catalog_grant_audit WHERE sequence <= NEW.sequence-1000;
END;
DELETE FROM catalog_grant_audit WHERE sequence <= (SELECT coalesce(max(sequence),0)-1000 FROM catalog_grant_audit);
