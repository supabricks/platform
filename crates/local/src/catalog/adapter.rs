//! Strict OSS UC 2.1 adapter. No ambient proxy, redirects, retries or raw errors.
use super::{
    http::Probe,
    metadata::{Code, Fault, Namespace, RemoteResult},
};
use serde_json::{Value, json};
use std::time::{Duration, Instant};
#[derive(Clone)]
pub(crate) struct Adapter {
    pub probe: Probe,
    pub provider_id: String,
}
impl Adapter {
    fn request(&self, method: &str, path: &str, body: Option<Value>) -> RemoteResult<Value> {
        self.owned_request(method, path, body, None)
    }
    pub(crate) fn owned_request(
        &self,
        method: &str,
        path: &str,
        body: Option<Value>,
        id: Option<&str>,
    ) -> RemoteResult<Value> {
        let token = super::config::token(&self.probe.token_file)
            .map_err(|_| Fault::new(Code::Unavailable, "catalog credentials unavailable"))?;
        let mut tls = ureq::tls::TlsConfig::builder();
        if let Some(p) = &self.probe.ca_file {
            let certs = super::config::certificates(p)
                .map_err(|_| Fault::new(Code::Unavailable, "catalog CA unavailable"))?;
            tls = tls.root_certs(ureq::tls::RootCerts::new_with_certs(&certs));
        }
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_millis(750)))
            .tls_config(tls.build())
            .build()
            .into();
        let url = format!(
            "{}/api/2.1/unity-catalog/{path}",
            self.probe.endpoint.trim_end_matches('/')
        );
        let response = if method == "POST" {
            let mut request = agent.post(url.clone());
            if let Some(id) = id {
                request = request.header("X-Supabricks-Table-Id", id);
            }
            request
                .header("Content-Type", "application/json")
                .header("Authorization", format!("Bearer {token}"))
                .send_json(body.unwrap())
        } else if method == "DELETE" {
            agent
                .delete(url)
                .header("Authorization", format!("Bearer {token}"))
                .header("X-Supabricks-Table-Id", id.unwrap())
                .call()
        } else {
            agent
                .get(url)
                .header("Authorization", format!("Bearer {token}"))
                .call()
        };
        let mut response = response.map_err(|_| {
            Fault::new(
                if method == "POST" {
                    Code::AmbiguousMutation
                } else {
                    Code::Unavailable
                },
                "catalog request failed; writes are never retried",
            )
        })?;
        let status = response.status().as_u16();
        if status == 404 {
            return Err(Fault::new(
                Code::NotFound,
                "catalog object no longer exists",
            ));
        }
        if status == 409 {
            return Err(Fault::new(
                Code::NameCollision,
                "catalog name already exists; ownership was not adopted",
            ));
        }
        if !(200..300).contains(&status) {
            return Err(Fault::new(
                if method == "POST" {
                    Code::AmbiguousMutation
                } else {
                    Code::Unavailable
                },
                "catalog rejected the request",
            ));
        }
        if method == "DELETE" {
            return Ok(serde_json::json!({}));
        }
        let bytes = response
            .body_mut()
            .with_config()
            .limit(65536)
            .read_to_vec()
            .map_err(|_| Fault::new(Code::InvalidResponse, "catalog response exceeds its bound"))?;
        serde_json::from_slice(&bytes)
            .map_err(|_| Fault::new(Code::InvalidResponse, "invalid catalog response"))
    }
    pub fn verify(&self) -> RemoteResult<()> {
        self.probe.clone().run().map(|_| ()).map_err(|code| {
            Fault::new(
                if code == "metastore_identity_changed" {
                    Code::IdentityChanged
                } else {
                    Code::Unavailable
                },
                "catalog health, authentication or metastore identity check failed",
            )
        })
    }
    fn object(
        &self,
        path: &str,
        name: &str,
        id: Option<&str>,
        parent: Option<(&str, &str)>,
    ) -> RemoteResult<String> {
        let v = self.request("GET", path, None)?;
        let actual = v[if parent.is_some() { "schema_id" } else { "id" }]
            .as_str()
            .filter(|s| s.parse::<supabricks_core::resource::ProjectId>().is_ok())
            .ok_or_else(|| Fault::new(Code::InvalidResponse, "catalog object has no UUID"))?;
        if v["name"] != name
            || id.is_some_and(|id| id != actual)
            || parent.is_some_and(|(key, name)| v[key] != name)
        {
            return Err(Fault::new(
                Code::IdentityChanged,
                "catalog object identity or alias changed",
            ));
        }
        Ok(actual.into())
    }
    // Each creation is a separate durable checkpoint. Existing names without a
    // recorded UUID are collisions, even when their UC properties claim ownership.
    pub fn namespace(&self, mut n: Namespace) -> RemoteResult<Namespace> {
        let started = Instant::now();
        self.verify()?;
        let catalog = format!("catalogs/{}", n.catalog);
        if let Some(id) = &n.catalog_id {
            self.object(&catalog, &n.catalog, Some(id), None)?;
        } else {
            match self.object(&catalog, &n.catalog, None, None) {
                Err(e) if matches!(e.code, Code::NotFound) => (),
                Err(e) => return Err(e),
                Ok(_) => {
                    return Err(Fault::new(
                        Code::NameCollision,
                        "default catalog name belongs to an untracked object",
                    ));
                }
            }
            if started.elapsed() > Duration::from_secs(4) {
                return Err(Fault::new(
                    Code::Unavailable,
                    "catalog request deadline exceeded",
                ));
            }
            let v = self.request("POST", "catalogs", Some(json!({"name":n.catalog})))?;
            n.catalog_id = Some(created_id(&v, &n.catalog, "id")?);
            n.state = "catalog_ready".into();
            return Ok(n);
        }
        let schema = format!("schemas/{}.{}", n.catalog, n.schema);
        if let Some(id) = &n.schema_id {
            self.object(
                &schema,
                &n.schema,
                Some(id),
                Some(("catalog_name", &n.catalog)),
            )?;
        } else {
            match self.object(&schema, &n.schema, None, Some(("catalog_name", &n.catalog))) {
                Err(e) if matches!(e.code, Code::NotFound) => (),
                Err(e) => return Err(e),
                Ok(_) => {
                    return Err(Fault::new(
                        Code::NameCollision,
                        "default schema name belongs to an untracked object",
                    ));
                }
            }
            if started.elapsed() > Duration::from_secs(4) {
                return Err(Fault::new(
                    Code::Unavailable,
                    "catalog request deadline exceeded",
                ));
            }
            let v = self.request(
                "POST",
                "schemas",
                Some(json!({"name":n.schema,"catalog_name":n.catalog})),
            )?;
            if v["catalog_name"] != n.catalog {
                return Err(Fault::new(
                    Code::AmbiguousMutation,
                    "created schema parent differs from the request",
                ));
            }
            n.schema_id = Some(created_id(&v, &n.schema, "schema_id")?);
        }
        n.state = "ready".into();
        Ok(n)
    }
}
fn created_id(v: &Value, name: &str, key: &str) -> RemoteResult<String> {
    if v["name"] != name {
        return Err(Fault::new(
            Code::AmbiguousMutation,
            "created catalog object has an unexpected alias",
        ));
    }
    v[key]
        .as_str()
        .filter(|id| id.parse::<supabricks_core::resource::ProjectId>().is_ok())
        .map(str::to_owned)
        .ok_or_else(|| {
            Fault::new(
                Code::AmbiguousMutation,
                "catalog create response has no object UUID",
            )
        })
}
