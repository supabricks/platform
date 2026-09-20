use super::*;
use ureq::tls::{RootCerts, TlsConfig};

#[derive(Clone)]
pub struct Probe {
    pub endpoint: String,
    pub token_file: PathBuf,
    pub ca_file: Option<PathBuf>,
    pub expected_metastore: Option<String>,
}
pub struct Health {
    pub metastore_id: String,
}
impl Probe {
    pub fn run(self) -> std::result::Result<Health, &'static str> {
        let token = config::token(&self.token_file).map_err(|_| "credential_unavailable")?;
        let mut tls = TlsConfig::builder();
        if let Some(path) = self.ca_file {
            let certs = config::certificates(&path).map_err(|_| "invalid_ca")?;
            tls = tls.root_certs(RootCerts::new_with_certs(&certs));
        }
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .proxy(None)
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_millis(750)))
            .tls_config(tls.build())
            .build()
            .into();
        let base = self.endpoint.trim_end_matches('/');
        let get =
            |path: &str, authenticated: bool| -> std::result::Result<(u16, Value), &'static str> {
                let mut request = agent.get(format!("{base}/api/2.1/unity-catalog/{path}"));
                if authenticated {
                    request = request.header("Authorization", format!("Bearer {token}"));
                }
                let mut response = request.call().map_err(|_| "connection_failed")?;
                let status = response.status().as_u16();
                let body = response
                    .body_mut()
                    .with_config()
                    .limit(65536)
                    .read_to_vec()
                    .map_err(|_| "invalid_response")?;
                Ok((status, serde_json::from_slice(&body).unwrap_or(Value::Null)))
            };
        let (anonymous, _) = get("catalogs?max_results=1", false)?;
        if !matches!(anonymous, 401 | 403) {
            return Err("authentication_not_enforced");
        }
        let (status, catalogs) = get("catalogs?max_results=1", true)?;
        if matches!(status, 401 | 403) {
            return Err("credential_rejected");
        }
        if status != 200 || !catalogs["catalogs"].is_array() {
            return Err("catalog_api_incompatible");
        }
        let (status, summary) = get("metastore_summary", true)?;
        let id = summary["metastore_id"]
            .as_str()
            .ok_or("metastore_identity_missing")?;
        if status != 200 || id.parse::<supabricks_core::resource::ProjectId>().is_err() {
            return Err("metastore_identity_missing");
        }
        if self
            .expected_metastore
            .as_ref()
            .is_some_and(|expected| expected != id)
        {
            return Err("metastore_identity_changed");
        }
        Ok(Health {
            metastore_id: id.to_owned(),
        })
    }
}
