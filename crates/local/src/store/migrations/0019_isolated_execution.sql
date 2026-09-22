-- Runs are never resumed after restart; authorization admissions are immutable.
CREATE TABLE isolated_executions (
 id TEXT PRIMARY KEY REFERENCES authorization_executions(id),
 token_hash TEXT NOT NULL,
 context_json TEXT NOT NULL,
 datasets_json TEXT NOT NULL,
 catalog_revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('preparing','running','finished','failed')),
 result_json TEXT,
 runtime_identity TEXT
);
