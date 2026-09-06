-- Every executable is gated until this evidence commits. Roles include shared
-- storage and the supervisor, so they cannot be keyed solely by endpoint.
CREATE TABLE native_processes (
    role TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
);
