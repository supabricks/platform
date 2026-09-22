-- Branch-specific data capabilities never follow deployment roles or UC SELECT.
CREATE TABLE data_grants (
 deployment TEXT NOT NULL REFERENCES deployments(id),
 branch TEXT NOT NULL REFERENCES branches(id), subject TEXT NOT NULL,
 capability TEXT NOT NULL CHECK(capability IN ('read','write','ddl','copy_source','receive','share')),
 PRIMARY KEY(deployment,branch,subject,capability)
);
CREATE TABLE governed_branches (
 branch TEXT PRIMARY KEY REFERENCES branches(id),
 state TEXT NOT NULL CHECK(state IN ('quarantined','ready'))
);
CREATE TABLE data_operations (
 id TEXT PRIMARY KEY, deployment TEXT NOT NULL REFERENCES deployments(id),
 branch TEXT NOT NULL REFERENCES branches(id), actor TEXT NOT NULL REFERENCES identity_principals(id),
 context_json TEXT NOT NULL, token_hash TEXT NOT NULL, policy_revision INTEGER NOT NULL,
 branch_revision INTEGER NOT NULL, capability TEXT NOT NULL, request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
 UNIQUE(deployment,actor,request_key)
);
