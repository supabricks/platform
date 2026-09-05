//! Storage-controller client — exactly the calls mk-compute.sh makes,
//! idempotent on replay (already-exists is success; the reconciler retries).

use anyhow::{Context, bail};
use serde_json::json;
use supabricks_core::resource::PgMajor;

/// A non-success storage-controller response with its HTTP status, so
/// callers can classify user-error 4xx as terminal instead of retrying
/// forever (review 002 P1: bogus raw LSNs).
#[derive(Debug)]
pub struct StorconHttp {
    pub status: u16,
    pub body: String,
}

impl std::fmt::Display for StorconHttp {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "HTTP {}: {}", self.status, self.body)
    }
}

impl std::error::Error for StorconHttp {}

#[derive(Clone)]
pub struct Storcon {
    base: String,
    pg_major: PgMajor,
    http: reqwest::Client,
}

impl Storcon {
    pub fn new(base: impl Into<String>, pg_major: PgMajor) -> Self {
        Self {
            base: base.into(),
            pg_major,
            // Timeouts are mandatory (found live, review 003): a client with
            // none lets one hung request park an object's reconcile future
            // FOREVER — kube-rs dedups events for in-flight objects, so the
            // CR never reconciles again until the operator restarts. 30s
            // covers the slowest legitimate call (tenant create → initdb).
            http: reqwest::Client::builder()
                .connect_timeout(std::time::Duration::from_secs(5))
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .expect("reqwest client"),
        }
    }

    pub fn pg_major(&self) -> PgMajor {
        self.pg_major
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
            code => bail!(
                "create_tenant {tenant_id}: HTTP {code}: {}",
                r.text().await?
            ),
        }
    }

    pub async fn create_timeline(
        &self,
        tenant_id: &str,
        timeline_id: &str,
        ancestor: Option<&str>,
        ancestor_start_lsn: Option<&str>,
    ) -> anyhow::Result<()> {
        let mut body =
            json!({"new_timeline_id": timeline_id, "pg_version": u16::from(self.pg_major)});
        if let Some(a) = ancestor {
            body["ancestor_timeline_id"] = json!(a);
        }
        if let Some(lsn) = ancestor_start_lsn {
            body["ancestor_start_lsn"] = json!(lsn);
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
            code => Err(anyhow::Error::new(StorconHttp {
                status: code,
                body: r.text().await.unwrap_or_default(),
            })
            .context(format!("create_timeline {timeline_id}"))),
        }
    }

    /// Pageserver-ingested head of a timeline (None on any failure).
    pub async fn timeline_last_record_lsn(
        &self,
        tenant_id: &str,
        timeline_id: &str,
    ) -> Option<String> {
        let r = self
            .http
            .get(format!(
                "{}/v1/tenant/{tenant_id}/timeline/{timeline_id}",
                self.base
            ))
            .send()
            .await
            .ok()?;
        let v: serde_json::Value = r.json().await.ok()?;
        v["last_record_lsn"].as_str().map(String::from)
    }

    /// Resolve an RFC 3339 timestamp to the nearest LSN on a timeline
    /// (pageserver get_lsn_by_timestamp, proxied by the controller).
    pub async fn lsn_by_timestamp(
        &self,
        tenant_id: &str,
        timeline_id: &str,
        ts: &str,
    ) -> anyhow::Result<String> {
        let url = reqwest::Url::parse_with_params(
            &format!(
                "{}/v1/tenant/{tenant_id}/timeline/{timeline_id}/get_lsn_by_timestamp",
                self.base
            ),
            [("timestamp", ts)],
        )
        .context("get_lsn_by_timestamp url")?;
        let r = self
            .http
            .get(url)
            .send()
            .await
            .context("storcon get_lsn_by_timestamp")?;
        let code = r.status().as_u16();
        let body = r.text().await.unwrap_or_default();
        if !(200..300).contains(&code) {
            bail!("get_lsn_by_timestamp: HTTP {code}: {body}");
        }
        // Older pageservers return a bare LSN string; newer ones
        // {"kind": past|present|future|nodata, "lsn": ...}.
        let v: serde_json::Value =
            serde_json::from_str(&body).unwrap_or_else(|_| json!(body.trim()));
        match &v {
            serde_json::Value::String(s) => Ok(s.trim_matches('"').to_string()),
            obj => {
                let kind = obj["kind"].as_str().unwrap_or("");
                match obj["lsn"].as_str() {
                    Some(l) if kind != "future" && kind != "nodata" => Ok(l.to_string()),
                    _ => bail!(
                        "timestamp resolves to no usable LSN (kind={kind}); \
                         is it within the parent's history?"
                    ),
                }
            }
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
            code => bail!(
                "delete_tenant {tenant_id}: HTTP {code}: {}",
                r.text().await?
            ),
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
            code => bail!(
                "delete_timeline {timeline_id}: HTTP {code}: {}",
                r.text().await?
            ),
        }
    }
}
