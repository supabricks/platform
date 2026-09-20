CREATE TABLE catalog_publications (
 id TEXT PRIMARY KEY,
 deployment_id TEXT NOT NULL REFERENCES deployments(id),
 branch_id TEXT NOT NULL REFERENCES branches(id),
 epoch_id TEXT NOT NULL REFERENCES snapshots(epoch_id),
 request_key TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('registering','published','retiring','retired')),
 record_json TEXT NOT NULL,
 UNIQUE(deployment_id,request_key), UNIQUE(deployment_id,epoch_id)
);
CREATE INDEX catalog_publication_pending ON catalog_publications(state);
CREATE TABLE catalog_heads (
 deployment_id TEXT NOT NULL REFERENCES deployments(id),
 branch_id TEXT NOT NULL REFERENCES branches(id),
 revision INTEGER NOT NULL,
 publication_id TEXT REFERENCES catalog_publications(id),
 PRIMARY KEY(deployment_id,branch_id)
);
CREATE TABLE catalog_retention (
 publication_id TEXT PRIMARY KEY REFERENCES catalog_publications(id),
 epoch_id TEXT NOT NULL REFERENCES snapshots(epoch_id)
);
CREATE INDEX catalog_retention_epoch ON catalog_retention(epoch_id);
CREATE TABLE catalog_publication_refs (
 publication_id TEXT NOT NULL REFERENCES catalog_publications(id),
 reference_key TEXT NOT NULL,
 PRIMARY KEY(publication_id,reference_key)
);
