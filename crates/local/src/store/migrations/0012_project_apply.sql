-- PK04: intent before effects, one pending apply per deployment, immutable revisions.
CREATE TABLE project_applies (
 id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
 request_key TEXT NOT NULL, state TEXT NOT NULL,
 record_json TEXT NOT NULL, UNIQUE(deployment_id,request_key)
);
CREATE UNIQUE INDEX project_one_apply ON project_applies(deployment_id)
 WHERE state IN ('queued','preparing','activating');
CREATE TABLE deployment_resources (
 deployment_id TEXT NOT NULL REFERENCES deployments(id), logical TEXT NOT NULL,
 record_json TEXT NOT NULL, PRIMARY KEY(deployment_id,logical)
);
CREATE TABLE deployment_revisions (
 id TEXT PRIMARY KEY REFERENCES project_applies(id),
 deployment_id TEXT NOT NULL REFERENCES deployments(id), record_json TEXT NOT NULL
);
CREATE TABLE deployment_active (
 deployment_id TEXT PRIMARY KEY REFERENCES deployments(id),
 revision_id TEXT NOT NULL REFERENCES deployment_revisions(id)
);
