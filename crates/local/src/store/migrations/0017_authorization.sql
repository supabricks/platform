-- UC09.2: deployment-scoped policy; no implicit grants to external identities.
CREATE TABLE authorization_policy (
 deployment TEXT PRIMARY KEY REFERENCES deployments(id), revision INTEGER NOT NULL CHECK(revision>0)
);
INSERT INTO authorization_policy SELECT id,1 FROM deployments;
CREATE TRIGGER authorization_new_deployment AFTER INSERT ON deployments BEGIN
 INSERT INTO authorization_policy VALUES (NEW.id,1);
END;
CREATE TABLE authorization_roles (
 deployment TEXT NOT NULL REFERENCES deployments(id), subject TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('viewer','editor','administrator')),
 PRIMARY KEY(deployment,subject)
);
CREATE TABLE authorization_grants (
 deployment TEXT NOT NULL REFERENCES deployments(id), subject TEXT NOT NULL,
 capability TEXT NOT NULL CHECK(capability IN ('execute','stop_any','act_as')),
 effective_principal TEXT NOT NULL, source_revision TEXT NOT NULL,
 PRIMARY KEY(deployment,subject,capability,effective_principal,source_revision)
);
CREATE TABLE authorization_sources (
 deployment TEXT NOT NULL REFERENCES deployments(id), revision TEXT NOT NULL,
 asset TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('sql','notebook')),
 contents TEXT NOT NULL, actor TEXT NOT NULL REFERENCES identity_principals(id),
 PRIMARY KEY(deployment,revision)
);
CREATE TABLE authorization_heads (
 deployment TEXT NOT NULL REFERENCES deployments(id), asset TEXT NOT NULL, revision TEXT NOT NULL,
 PRIMARY KEY(deployment,asset), FOREIGN KEY(deployment,revision) REFERENCES authorization_sources(deployment,revision)
);
CREATE TABLE authorization_executions (
 id TEXT PRIMARY KEY, deployment TEXT NOT NULL REFERENCES deployments(id),
 actor TEXT NOT NULL REFERENCES identity_principals(id), effective_principal TEXT NOT NULL REFERENCES identity_principals(id),
 source_revision TEXT NOT NULL, policy_revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('admitted','cancelled')),
 FOREIGN KEY(deployment,source_revision) REFERENCES authorization_sources(deployment,revision)
);
CREATE TABLE authorization_mutations (
 deployment TEXT NOT NULL REFERENCES deployments(id), actor TEXT NOT NULL REFERENCES identity_principals(id),
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(deployment,actor,request_key)
);
CREATE TABLE authorization_audit (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
 realm TEXT NOT NULL REFERENCES realms(id), deployment TEXT NOT NULL REFERENCES deployments(id),
 actor TEXT NOT NULL REFERENCES identity_principals(id), effective_principal TEXT NOT NULL REFERENCES identity_principals(id),
 policy_revision INTEGER NOT NULL, action TEXT NOT NULL, request_key TEXT NOT NULL, target TEXT NOT NULL
);
-- Changes in group expansion/disabled identities invalidate saved policy plans.
CREATE TRIGGER authorization_membership_added AFTER INSERT ON identity_memberships BEGIN
 UPDATE authorization_policy SET revision=revision+1;
END;
CREATE TRIGGER authorization_membership_removed AFTER DELETE ON identity_memberships BEGIN
 UPDATE authorization_policy SET revision=revision+1;
END;
CREATE TRIGGER authorization_principal_disabled AFTER UPDATE OF disabled ON identity_principals
WHEN OLD.disabled!=NEW.disabled BEGIN
 UPDATE authorization_policy SET revision=revision+1;
END;
