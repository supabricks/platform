//! Direct API for the pinned single-owner pageserver. POST success is the
//! timeline durability barrier; GET visibility alone is not a create receipt.
use super::http::Http;
use crate::store::{
    BranchRecord, Result,
    error::{conflict, invalid},
};
use serde_json::{Value, json};
use supabricks_core::lsn::Lsn;
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
    pub fn detail(&self, b: &BranchRecord) -> Result<(u16, Value)> {
        self.request(
            "GET",
            &format!(
                "tenant/{}/timeline/{}",
                b.branch.tenant_id, b.branch.timeline_id
            ),
            None,
        )
    }
    pub fn lease(&self, b: &BranchRecord, lsn: Lsn) -> Result<bool> {
        let (code, body) = self.request(
            "POST",
            &format!(
                "tenant/{}/timeline/{}/lsn_lease",
                b.branch.tenant_id, b.branch.timeline_id
            ),
            Some(&json!({"lsn":lsn})),
        )?;
        if code == 200 && body.get("valid_until").is_some() {
            return Ok(true);
        }
        let (detail_code, detail) = self.detail(b)?;
        if detail_code == 200
            && detail["min_readable_lsn"]
                .as_str()
                .and_then(|s| s.parse::<Lsn>().ok())
                .is_some_and(|minimum| lsn < minimum)
        {
            return Err(invalid(
                "pinned branch point is no longer retained by the parent",
            ));
        }
        Ok(false)
    }
    pub fn ensure(&self, b: &BranchRecord, parent: Option<&BranchRecord>) -> Result<bool> {
        let tenant = &b.branch.tenant_id;
        let timeline = &b.branch.timeline_id;
        let (code,_)=self.request("PUT",&format!("tenant/{tenant}/location_config"),Some(&json!({"mode":"AttachedSingle","generation":self.generation,"tenant_conf":{"lazy_slru_download":true,"checkpoint_timeout":"1s","compaction_period":"5s"}})))?;
        if !(200..300).contains(&code) {
            return Ok(false);
        }
        if b.timeline_created {
            let (code, body) = self.detail(b)?;
            if code != 200 {
                return Ok(false);
            }
            if body["ancestor_lsn"] != json!(b.branch.ancestor_lsn)
                || body["ancestor_timeline_id"] != json!(parent.map(|p| &p.branch.timeline_id))
            {
                return Err(conflict(
                    "engine timeline ancestry differs from durable local pins",
                ));
            }
            return Ok(body["state"] == "Active");
        }
        let mut body = json!({"new_timeline_id":timeline,"pg_version":17});
        if let Some(parent) = parent {
            let point = b
                .branch
                .ancestor_lsn
                .ok_or_else(|| conflict("child branch point has not been captured"))?;
            if !self.lease(parent, point)? {
                return Ok(false);
            }
            body["ancestor_timeline_id"] = json!(parent.branch.timeline_id);
            body["ancestor_start_lsn"] = json!(point);
        }
        let (code, _) = self.request("POST", &format!("tenant/{tenant}/timeline"), Some(&body))?;
        match code {
            200..=299 => Ok(true),
            400 | 406 | 409 => Err(invalid(
                "pageserver rejected the exact timeline creation parameters",
            )),
            _ => Ok(false),
        }
    }
    pub fn delete(&self, b: &BranchRecord) -> Result<bool> {
        let path = format!(
            "tenant/{}/timeline/{}",
            b.branch.tenant_id, b.branch.timeline_id
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
