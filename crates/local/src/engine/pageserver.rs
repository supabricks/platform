//! Direct pageserver API for the pinned, single-owner local profile.
//! This is deliberately not the storage-controller API adapter.
use super::http::Http;
use crate::store::{BranchRecord, Result, error::conflict};
use serde_json::{Value, json};
pub struct Pageserver {
    pub port: u16,
    pub token: String,
    pub generation: i64,
}
impl Pageserver {
    pub fn request(&self, method: &str, path: &str, body: Option<&Value>) -> Result<(u16, Value)> {
        Http::default().json(
            self.port,
            method,
            &format!("/v1/{path}"),
            &[("Authorization", &format!("Bearer {}", self.token))],
            body,
        )
    }
    pub fn ensure(&self, branch: &BranchRecord) -> Result<bool> {
        if branch.branch.parent_id.is_some() {
            return Err(conflict(
                "explicit-LSN child branching is implemented in P04",
            ));
        }
        let tenant = &branch.branch.tenant_id;
        let timeline = &branch.branch.timeline_id;
        let (code, _) = self.request("PUT", &format!("tenant/{tenant}/location_config"), Some(&json!({
            "mode":"AttachedSingle", "generation":self.generation,
            "tenant_conf":{"lazy_slru_download":true,"checkpoint_timeout":"1s","compaction_period":"5s"}
        })))?;
        if !(200..300).contains(&code) {
            return Ok(false);
        }
        let (code, _) =
            self.request("GET", &format!("tenant/{tenant}/timeline/{timeline}"), None)?;
        if code == 200 {
            return Ok(true);
        }
        if code != 404 {
            return Ok(false);
        }
        let (code, _) = self.request(
            "POST",
            &format!("tenant/{tenant}/timeline"),
            Some(&json!({"new_timeline_id":timeline,"pg_version":17})),
        )?;
        Ok((200..300).contains(&code))
    }
    pub fn delete(&self, branch: &BranchRecord) -> Result<bool> {
        let path = format!(
            "tenant/{}/timeline/{}",
            branch.branch.tenant_id, branch.branch.timeline_id
        );
        let (code, _) = self.request("GET", &path, None)?;
        if code == 404 {
            return Ok(true);
        }
        if code != 200 {
            return Ok(false);
        }
        self.request("DELETE", &path, None)?;
        Ok(false)
    }
}
