-- I00: immutable source references and origin-scoped import evidence.
CREATE TABLE ingest_identity (id INTEGER PRIMARY KEY CHECK(id=1), origin TEXT NOT NULL);
INSERT INTO ingest_identity SELECT 1, id FROM analytics_installation;
CREATE TABLE catalog_migrations (
    version INTEGER PRIMARY KEY, source_sha256 TEXT NOT NULL, release_identity TEXT NOT NULL
);
CREATE TABLE ingest_sources (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    generation INTEGER NOT NULL, display_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('receiving','staged','disposed','expired','interrupted')),
    bytes INTEGER NOT NULL DEFAULT 0 CHECK(bytes BETWEEN 0 AND 104857600),
    sha256 TEXT, expires_at_ms INTEGER NOT NULL, payload_deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(id,project_id),
    CHECK(state!='staged' OR (sha256 IS NOT NULL AND length(sha256)=64))
);
CREATE TABLE ingest_jobs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    branch_id TEXT NOT NULL, branch_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL, request_key TEXT NOT NULL,
    request TEXT NOT NULL, fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','loading','reconciling','succeeded','failed','cancelled')),
    attempt INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
    worker TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
    parsed_rows INTEGER NOT NULL DEFAULT 0 CHECK(parsed_rows>=0),
    copied_rows INTEGER NOT NULL DEFAULT 0 CHECK(copied_rows>=0 AND copied_rows<=parsed_rows),
    committed_rows INTEGER CHECK(committed_rows>=0), receipt TEXT,
    retryable INTEGER NOT NULL DEFAULT 0, source_released INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY(branch_id,project_id) REFERENCES branches(id,project_id),
    FOREIGN KEY(source_id,project_id) REFERENCES ingest_sources(id,project_id),
    UNIQUE(project_id,branch_id,request_key),
    CHECK((state='succeeded')=(committed_rows IS NOT NULL)),
    CHECK((state='succeeded')=(receipt IS NOT NULL))
);
CREATE INDEX ingest_jobs_source ON ingest_jobs(source_id,source_released);
CREATE INDEX ingest_jobs_active ON ingest_jobs(state);
