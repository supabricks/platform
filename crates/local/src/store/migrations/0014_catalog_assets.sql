-- UC02: additive metadata only. No existing project or UC object is rewritten.
CREATE TABLE catalog_namespaces (
 deployment_id TEXT NOT NULL REFERENCES deployments(id), provider_id TEXT NOT NULL,
 record_json TEXT NOT NULL, PRIMARY KEY(deployment_id,provider_id)
);
CREATE TABLE catalog_assets (
 id TEXT PRIMARY KEY, deployment_id TEXT NOT NULL REFERENCES deployments(id),
 project_id TEXT NOT NULL REFERENCES projects(id), branch_id TEXT NOT NULL REFERENCES branches(id),
 provider_id TEXT NOT NULL, resource_key TEXT NOT NULL, incarnation TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('postgres_table','delta_snapshot')),
 state TEXT NOT NULL CHECK(state IN ('active','stale')), record_json TEXT NOT NULL,
 UNIQUE(deployment_id,provider_id,resource_key,incarnation)
);
CREATE INDEX catalog_asset_owner ON catalog_assets(deployment_id,branch_id,kind,state);
