//! Storage-controller client — exactly the calls mk-compute.sh makes,
//! idempotent on replay (already-exists is success; the reconciler retries).

use anyhow::{Context, bail};
use serde_json::json;

#[derive(Clone)]
pub struct Storcon {
    base: String,
    http: reqwest::Client,
}

impl Storcon {
    pub fn new(base: impl Into<String>) -> Self {
        Self {
            base: base.into(),
            http: reqwest::Client::new(),
        }
    }

    pub async fn create_tenant(&self, tenant_id: &str) -> anyhow::Result<()> {
        let r = self
            .http
            .post(format!("{}/v1/tenant", self.base))
            .json(&json!({"new_tenant_id": tenant_id}))
            .send()
            .await
            .context("storcon create_tenant")?;
        match r.status().as_u16() {
            200..=299 | 409 => Ok(()),
            code => bail!("create_tenant {tenant_id}: HTTP {code}: {}", r.text().await?),
        }
    }

    pub async fn create_timeline(
        &self,
        tenant_id: &str,
        timeline_id: &str,
        ancestor: Option<&str>,
    ) -> anyhow::Result<()> {
        let mut body = json!({"new_timeline_id": timeline_id, "pg_version": 16});
        if let Some(a) = ancestor {
            body["ancestor_timeline_id"] = json!(a);
        }
        let r = self
            .http
            .post(format!("{}/v1/tenant/{tenant_id}/timeline", self.base))
            .json(&body)
            .send()
            .await
            .context("storcon create_timeline")?;
        match r.status().as_u16() {
            200..=299 | 409 => Ok(()),
            code => bail!("create_timeline {timeline_id}: HTTP {code}: {}", r.text().await?),
        }
    }

    pub async fn delete_tenant(&self, tenant_id: &str) -> anyhow::Result<()> {
        let r = self
            .http
            .delete(format!("{}/v1/tenant/{tenant_id}", self.base))
            .send()
            .await
            .context("storcon delete_tenant")?;
        match r.status().as_u16() {
            200..=299 | 404 => Ok(()),
            code => bail!("delete_tenant {tenant_id}: HTTP {code}: {}", r.text().await?),
        }
    }

    pub async fn delete_timeline(&self, tenant_id: &str, timeline_id: &str) -> anyhow::Result<()> {
        let r = self
            .http
            .delete(format!(
                "{}/v1/tenant/{tenant_id}/timeline/{timeline_id}",
                self.base
            ))
            .send()
            .await
            .context("storcon delete_timeline")?;
        match r.status().as_u16() {
            200..=299 | 404 => Ok(()),
            code => bail!("delete_timeline {timeline_id}: HTTP {code}: {}", r.text().await?),
        }
    }
}
