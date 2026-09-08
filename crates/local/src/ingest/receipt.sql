-- Protocol v1. Execute with table creation and COPY inside ONE PG transaction.
-- Values use driver parameters; identifiers use the driver's identifier API.
-- I01 must validate an existing schema/table definition, never silently repair it.
CREATE SCHEMA IF NOT EXISTS _supabricks;
REVOKE ALL ON SCHEMA _supabricks FROM PUBLIC;
CREATE TABLE IF NOT EXISTS _supabricks.ingest_receipts (
    version integer NOT NULL CHECK (version = 1),
    origin text NOT NULL CHECK (length(origin) = 32),
    project_id uuid NOT NULL, branch_id uuid NOT NULL, job_id uuid NOT NULL,
    source_sha256 text NOT NULL CHECK (length(source_sha256) = 64),
    mapping_fingerprint text NOT NULL CHECK (length(mapping_fingerprint) = 64),
    target_schema text NOT NULL, target_table text NOT NULL,
    target_oid oid NOT NULL, committed_rows bigint NOT NULL CHECK (committed_rows >= 0),
    PRIMARY KEY (origin, project_id, branch_id, job_id)
);
REVOKE ALL ON _supabricks.ingest_receipts FROM PUBLIC;
