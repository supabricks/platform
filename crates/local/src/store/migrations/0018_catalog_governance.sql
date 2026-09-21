-- UC09.3: administrative intents, never a replacement for live UC enforcement.
CREATE TABLE catalog_governance (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('dirty','ready','applying'))
);
INSERT INTO catalog_governance VALUES (1,1,'dirty');
CREATE TABLE catalog_principals (
 principal TEXT PRIMARY KEY REFERENCES identity_principals(id),
 subject TEXT NOT NULL UNIQUE, uc_id TEXT UNIQUE, provider TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','ready'))
);
CREATE TABLE catalog_grant_origins (
 publication TEXT NOT NULL REFERENCES catalog_publications(id), subject TEXT NOT NULL,
 publication_revision INTEGER NOT NULL, tables_json TEXT NOT NULL,
 PRIMARY KEY(publication,subject)
);
CREATE TABLE catalog_grant_plans (
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('planned','applying','applied','failed')),
 request_key TEXT UNIQUE
);
CREATE TABLE catalog_grant_audit (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
 actor TEXT NOT NULL REFERENCES identity_principals(id), action TEXT NOT NULL,
 target TEXT NOT NULL, revision INTEGER NOT NULL
);
CREATE TRIGGER catalog_governance_membership_add AFTER INSERT ON identity_memberships BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;
CREATE TRIGGER catalog_governance_membership_remove AFTER DELETE ON identity_memberships BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;
CREATE TRIGGER catalog_governance_disabled AFTER UPDATE OF disabled ON identity_principals
WHEN OLD.disabled!=NEW.disabled BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;
CREATE TRIGGER catalog_governance_publication_insert AFTER INSERT ON catalog_publications BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;
CREATE TRIGGER catalog_governance_publication_update AFTER UPDATE ON catalog_publications BEGIN
 UPDATE catalog_governance SET revision=revision+1,state='dirty';
END;
