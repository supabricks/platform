-- NE02: one daemon owns preparation, publication, leases and collection.
CREATE TABLE environment_generations (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    worktree TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('building','ready','invalid','deleting','removed')),
    record_json TEXT NOT NULL,
    UNIQUE(id,project_id,worktree)
);
CREATE TABLE environment_operations (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    worktree TEXT NOT NULL, request_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','initializing','preparing','verifying','ready','failed','cancelled')),
    record_json TEXT NOT NULL, UNIQUE(project_id,worktree,request_key)
);
CREATE UNIQUE INDEX environment_one_operation_per_worktree
    ON environment_operations(project_id,worktree)
    WHERE state IN ('queued','initializing','preparing','verifying');
CREATE TABLE environment_active (
    project_id TEXT NOT NULL, worktree TEXT NOT NULL, generation_id TEXT NOT NULL,
    PRIMARY KEY(project_id,worktree),
    FOREIGN KEY(generation_id,project_id,worktree) REFERENCES environment_generations(id,project_id,worktree)
);
CREATE TABLE environment_leases (
    id TEXT PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES environment_generations(id),
    holder TEXT NOT NULL, daemon_generation INTEGER NOT NULL,
    UNIQUE(generation_id,holder)
);
