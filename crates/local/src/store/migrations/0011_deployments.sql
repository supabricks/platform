-- PK03: preserve runtime projects and all existing resource IDs.
CREATE TABLE realms (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE workspaces (id TEXT PRIMARY KEY, realm_id TEXT NOT NULL REFERENCES realms(id), name TEXT NOT NULL, UNIQUE(realm_id,name));
CREATE TABLE principals (id TEXT PRIMARY KEY, realm_id TEXT NOT NULL REFERENCES realms(id), provider TEXT NOT NULL CHECK(provider='local-owner'));
INSERT INTO realms VALUES (lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(6))), 'local');
INSERT INTO workspaces SELECT lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(6))),id,'local' FROM realms;
INSERT INTO principals SELECT lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(6))),id,'local-owner' FROM realms;
CREATE TABLE project_definitions (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE deployments (
 id TEXT PRIMARY KEY, definition_id TEXT NOT NULL REFERENCES project_definitions(id),
 workspace_id TEXT NOT NULL REFERENCES workspaces(id), runtime_project_id TEXT NOT NULL UNIQUE REFERENCES projects(id),
 target TEXT NOT NULL, legacy INTEGER NOT NULL CHECK(legacy IN (0,1)), revision INTEGER NOT NULL DEFAULT 1,
 actor_id TEXT NOT NULL REFERENCES principals(id), effective_principal_id TEXT NOT NULL REFERENCES principals(id)
);
CREATE TABLE worktree_bindings (
 path TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
 source_format INTEGER NOT NULL CHECK(source_format IN (1,2)), device TEXT, inode TEXT,
 actor_id TEXT NOT NULL REFERENCES principals(id), effective_principal_id TEXT NOT NULL REFERENCES principals(id)
);
CREATE TABLE binding_operations (
 definition_id TEXT NOT NULL REFERENCES project_definitions(id), request_key TEXT NOT NULL,
 request TEXT NOT NULL, deployment_id TEXT NOT NULL REFERENCES deployments(id),
 actor_id TEXT NOT NULL REFERENCES principals(id), effective_principal_id TEXT NOT NULL REFERENCES principals(id),
 PRIMARY KEY(definition_id,request_key)
);
INSERT INTO project_definitions SELECT id,name FROM projects;
INSERT INTO deployments SELECT
 lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(2)))||'-'||lower(hex(randomblob(6))),
 p.id,w.id,p.id,'local',1,1,a.id,a.id FROM projects p CROSS JOIN workspaces w CROSS JOIN principals a;
-- Conflicting paths fail the transaction instead of choosing a project silently.
INSERT INTO worktree_bindings
 SELECT known.path,d.id,1,NULL,NULL,d.actor_id,d.effective_principal_id FROM (
 SELECT path,project_id FROM worktrees
 UNION SELECT worktree,project_id FROM environment_generations
 UNION SELECT worktree,project_id FROM environment_operations
 UNION SELECT worktree,project_id FROM environment_active
 ) known JOIN deployments d ON d.runtime_project_id=known.project_id;
