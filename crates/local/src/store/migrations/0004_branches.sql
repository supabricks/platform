ALTER TABLE branches ADD COLUMN ancestor_lsn TEXT;
ALTER TABLE branches ADD COLUMN expires_at_ms INTEGER;
ALTER TABLE branches ADD COLUMN expired INTEGER NOT NULL DEFAULT 0 CHECK(expired IN (0,1));
CREATE TABLE project_defaults (
    project_id TEXT PRIMARY KEY REFERENCES projects(id), branch_id TEXT NOT NULL,
    FOREIGN KEY(branch_id,project_id) REFERENCES branches(id,project_id)
);
CREATE TABLE app_credentials (
    endpoint_id TEXT PRIMARY KEY REFERENCES endpoints(id), password TEXT NOT NULL
);
INSERT INTO app_credentials SELECT endpoint_id,lower(hex(randomblob(32))) FROM credentials;
CREATE TABLE branch_pins (
    child_id TEXT PRIMARY KEY REFERENCES branches(id), parent_id TEXT NOT NULL REFERENCES branches(id),
    point TEXT NOT NULL, deadline_ms INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE parent_wakes (
    parent_id TEXT PRIMARY KEY REFERENCES branches(id), revision INTEGER NOT NULL
);
CREATE TABLE operation_errors (
    operation_id TEXT PRIMARY KEY REFERENCES operations(id), detail TEXT NOT NULL,
    terminal INTEGER NOT NULL CHECK(terminal IN (0,1))
);
ALTER TABLE branches ADD COLUMN timeline_created INTEGER NOT NULL DEFAULT 0;
UPDATE branches SET timeline_created=1 WHERE EXISTS (
 SELECT 1 FROM operations o JOIN checkpoints c ON c.operation_id=o.id
 WHERE o.branch_id=branches.id AND json_extract(o.steps,'$['||c.step||']')='ensure_timeline'
);
