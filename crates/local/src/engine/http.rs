//! Bounded loopback requests. Never inherit proxy configuration or follow redirects.
use crate::store::{Result, error::conflict};
use serde_json::Value;
use std::time::Duration;
pub struct Http {
    agent: ureq::Agent,
}
impl Default for Http {
    fn default() -> Self {
        Self {
            agent: ureq::Agent::config_builder()
                .proxy(None)
                .http_status_as_error(false)
                .max_redirects(0)
                .timeout_global(Some(Duration::from_secs(2)))
                .build()
                .into(),
        }
    }
}
impl Http {
    pub fn request(
        &self,
        port: u16,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: &[u8],
    ) -> Result<(u16, Vec<u8>)> {
        let mut req = ureq::http::Request::builder()
            .method(method)
            .uri(format!("http://127.0.0.1:{port}{path}"));
        for (name, value) in headers {
            req = req.header(*name, *value);
        }
        let req = req
            .body(body)
            .map_err(|_| conflict("invalid local HTTP request"))?;
        let mut response = self
            .agent
            .run(req)
            .map_err(|_| conflict("local service unavailable"))?;
        let code = response.status().as_u16();
        let bytes = response
            .body_mut()
            .with_config()
            .limit(4 * 1024 * 1024)
            .read_to_vec()
            .map_err(|_| conflict("invalid local service response"))?;
        Ok((code, bytes))
    }
    pub fn json(
        &self,
        port: u16,
        method: &str,
        path: &str,
        headers: &[(&str, &str)],
        body: Option<&Value>,
    ) -> Result<(u16, Value)> {
        let bytes = body
            .map(serde_json::to_vec)
            .transpose()?
            .unwrap_or_default();
        let mut headers = headers.to_vec();
        headers.push(("Content-Type", "application/json"));
        let (code, bytes) = self.request(port, method, path, &headers, &bytes)?;
        let value = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        Ok((code, value))
    }
}
