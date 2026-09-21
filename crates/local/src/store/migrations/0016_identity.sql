-- Local-owner remains the only enabled product profile. Identity state does not
-- grant any remote project/catalog/PG rights or change existing resource IDs.
CREATE TABLE identity_realm (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE REFERENCES realms(id),
    local_owner TEXT NOT NULL UNIQUE, session_epoch INTEGER NOT NULL DEFAULT 1,
    bootstrap_principal TEXT
);
INSERT INTO identity_realm(singleton,id,local_owner)
SELECT 1,r.id,p.id FROM realms r JOIN principals p ON p.realm_id=r.id
WHERE r.name='local' AND p.provider='local-owner';
CREATE TABLE identity_principals (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('local_owner','user','service')),
    label TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1))
);
INSERT INTO identity_principals(id,kind,label) SELECT local_owner,'local_owner','Local owner' FROM identity_realm;
CREATE TABLE identity_subjects (
    issuer TEXT NOT NULL, subject TEXT NOT NULL, principal TEXT NOT NULL REFERENCES identity_principals(id),
    PRIMARY KEY(issuer,subject), UNIQUE(principal)
);
CREATE TABLE identity_groups (id TEXT PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE identity_memberships (
    group_id TEXT NOT NULL REFERENCES identity_groups(id), principal TEXT NOT NULL REFERENCES identity_principals(id),
    PRIMARY KEY(group_id,principal)
);
CREATE TABLE identity_providers (id TEXT PRIMARY KEY, config TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE identity_logins (
    state_hash TEXT PRIMARY KEY, binding_hash TEXT NOT NULL, provider TEXT NOT NULL REFERENCES identity_providers(id),
    redirect TEXT NOT NULL, nonce TEXT NOT NULL, verifier TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('browser','cli')), expires_ms INTEGER NOT NULL
);
CREATE TABLE identity_sessions (
    token_hash TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES identity_principals(id),
    csrf_hash TEXT NOT NULL, channel TEXT NOT NULL CHECK(channel IN ('browser','cli','service')),
    scopes TEXT NOT NULL, expires_ms INTEGER NOT NULL, epoch INTEGER NOT NULL,
    provider TEXT REFERENCES identity_providers(id), access_token TEXT,
    subject TEXT, client_id TEXT
);
CREATE INDEX identity_sessions_principal ON identity_sessions(principal);
CREATE TABLE identity_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
    actor TEXT NOT NULL REFERENCES identity_principals(id), effective_principal TEXT NOT NULL REFERENCES identity_principals(id),
    action TEXT NOT NULL, target TEXT NOT NULL
);
