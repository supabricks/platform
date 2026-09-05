CREATE TABLE owner (id INTEGER PRIMARY KEY CHECK(id = 1), generation INTEGER NOT NULL);
INSERT INTO owner VALUES (1, 0);
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE branches (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
    tenant_id TEXT NOT NULL, timeline_id TEXT NOT NULL UNIQUE,
    parent_id TEXT REFERENCES branches(id), revision INTEGER NOT NULL CHECK(revision > 0),
    desired TEXT NOT NULL CHECK(desired IN ('running','suspended','deleted')),
    observed_revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE(id, project_id)
);
CREATE UNIQUE INDEX live_branch_names ON branches(project_id,name)
WHERE desired != 'deleted' OR observed_revision != revision;
CREATE TABLE endpoints (
    id TEXT PRIMARY KEY, branch_id TEXT NOT NULL UNIQUE REFERENCES branches(id),
    pg_major INTEGER NOT NULL CHECK(pg_major = 17)
);
CREATE TABLE ports (
    port INTEGER PRIMARY KEY CHECK(port BETWEEN 1 AND 65535),
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id),
    role TEXT NOT NULL CHECK(role IN ('sql','external_http','internal_http')),
    UNIQUE(endpoint_id, role)
);
CREATE TABLE worktrees (
    path TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), branch_id TEXT NOT NULL,
    FOREIGN KEY(branch_id, project_id) REFERENCES branches(id, project_id)
);
CREATE TABLE credentials (
    endpoint_id TEXT PRIMARY KEY REFERENCES endpoints(id), username TEXT NOT NULL, password TEXT NOT NULL
);
CREATE TABLE operations (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), request_key TEXT NOT NULL,
    request TEXT NOT NULL, branch_id TEXT NOT NULL REFERENCES branches(id),
    revision INTEGER NOT NULL, steps TEXT NOT NULL, next_step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','succeeded','superseded')),
    UNIQUE(project_id, request_key)
);
CREATE TABLE checkpoints (
    operation_id TEXT NOT NULL REFERENCES operations(id), step INTEGER NOT NULL,
    result TEXT NOT NULL, PRIMARY KEY(operation_id, step)
);
