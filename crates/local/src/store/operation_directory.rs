//! Every operation in a project as one ordered feed, and stored API replies.
use super::{Result, Store, error::missing};
use rusqlite::{OptionalExtension, params};

#[derive(Clone, Debug, PartialEq)]
pub struct OperationEntry {
    pub seq: i64,
    pub id: String,
    pub deployment_id: String,
    /// `native` (a database or branch change) or `apply` (a project apply).
    pub source: String,
    pub created_at_ms: i64,
    pub finished_at_ms: Option<i64>,
    /// The Idempotency-Key of the API request that started it, if one did.
    pub api_key: Option<String>,
}
/// A native operation's stored request, for its kind and key.
#[derive(Clone, Debug)]
pub struct NativeRequest {
    pub key: String,
    pub request: String,
    pub error: Option<String>,
}

fn entry(row: &rusqlite::Row) -> rusqlite::Result<OperationEntry> {
    Ok(OperationEntry {
        seq: row.get(0)?,
        id: row.get(1)?,
        deployment_id: row.get(2)?,
        source: row.get(3)?,
        created_at_ms: row.get(4)?,
        finished_at_ms: row.get(5)?,
        api_key: row.get(6)?,
    })
}
const COLUMNS: &str = "seq,id,deployment_id,source,created_at_ms,finished_at_ms,api_key";

impl Store {
    /// Newest first, strictly older than `before`. Returns at most `limit`.
    pub fn operation_entries(
        &self,
        deployment: &str,
        before: Option<i64>,
        limit: usize,
    ) -> Result<Vec<OperationEntry>> {
        Ok(self
            .db
            .prepare(&format!(
                "SELECT {COLUMNS} FROM operation_index WHERE deployment_id=?1 AND seq<?2 ORDER BY seq DESC LIMIT ?3"
            ))?
            .query_map(
                params![deployment, before.unwrap_or(i64::MAX), limit as i64],
                entry,
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn operation_entry(&self, deployment: &str, id: &str) -> Result<OperationEntry> {
        self.db
            .query_row(
                &format!("SELECT {COLUMNS} FROM operation_index WHERE deployment_id=?1 AND id=?2"),
                params![deployment, id],
                entry,
            )
            .optional()?
            .ok_or_else(|| missing("operation"))
    }
    pub fn set_operation_api_key(&self, id: &str, key: &str) -> Result<()> {
        self.db.execute(
            "UPDATE operation_index SET api_key=?2 WHERE id=?1 AND api_key IS NULL",
            params![id, key],
        )?;
        Ok(())
    }
    pub fn native_request(&self, id: &str) -> Result<NativeRequest> {
        self.db
            .query_row(
                "SELECT o.request_key,o.request,(SELECT detail FROM operation_errors e WHERE e.operation_id=o.id AND e.terminal=1) FROM operations o WHERE o.id=?1",
                [id],
                |r| {
                    Ok(NativeRequest {
                        key: r.get(0)?,
                        request: r.get(1)?,
                        error: r.get(2)?,
                    })
                },
            )
            .optional()?
            .ok_or_else(|| missing("operation"))
    }
    pub fn set_project_description(&self, deployment: &str, description: &str) -> Result<()> {
        self.db.execute(
            "UPDATE project_details SET description=?2 WHERE deployment_id=?1",
            params![deployment, description],
        )?;
        Ok(())
    }
    /// The stored reply for a key: its request digest and the reply.
    pub fn api_reply(&self, scope: &str, key: &str) -> Result<Option<(String, String)>> {
        Ok(self
            .db
            .query_row(
                "SELECT request_sha256,reply_json FROM api_idempotency WHERE scope=?1 AND request_key=?2",
                params![scope, key],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?)
    }
    pub fn save_api_reply(&self, scope: &str, key: &str, digest: &str, reply: &str) -> Result<()> {
        let now = chrono::Utc::now().timestamp_millis();
        self.db.execute(
            "DELETE FROM api_idempotency WHERE created_at_ms<?1",
            [now - 7 * 86_400_000],
        )?;
        self.db.execute(
            "INSERT OR IGNORE INTO api_idempotency VALUES (?1,?2,?3,?4,?5)",
            params![scope, key, digest, reply, now],
        )?;
        Ok(())
    }
}
