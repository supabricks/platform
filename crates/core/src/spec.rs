//! Compute-spec rendering with adapter-supplied runtime settings. The legacy
//! operator profile is golden-tested against the P0 JSON fixture.

use crate::{
    error::ValidationError,
    resource::{TenantId, TimelineId},
};
use serde_json::{Value, json};
use std::path::PathBuf;

/// Explicit paths; no installation layout or environment discovery in core.
pub struct ComputePaths {
    pub compute_ctl: PathBuf,
    pub postgres: PathBuf,
    pub data: PathBuf,
    pub config: PathBuf,
}

impl ComputePaths {
    /// Arguments are separate argv elements, never a shell command. The version
    /// is selected by the caller's postgres path (compute_ctl inspects it).
    pub fn command(&self, compute_id: &str, connstr: &str) -> anyhow::Result<Vec<String>> {
        let path = |p: &std::path::Path| -> anyhow::Result<String> {
            if !p.is_absolute() {
                anyhow::bail!("compute paths must be absolute");
            }
            Ok(p.to_str()
                .ok_or_else(|| anyhow::anyhow!("compute paths must be UTF-8"))?
                .to_owned())
        };
        Ok(vec![
            path(&self.compute_ctl)?,
            format!("--pgdata={}", path(&self.data)?),
            format!("--connstr={connstr}"),
            format!("--pgbin={}", path(&self.postgres)?),
            format!("--compute-id={compute_id}"),
            format!("--config={}", path(&self.config)?),
        ])
    }
}

const TEMPLATE: &str = include_str!("../assets/compute-spec-template.json");

pub struct SpecParams<'a> {
    pub tenant_id: &'a str,
    pub timeline_id: &'a str,
    /// PG classic md5 credential for the owner role: hex(md5(password+user)),
    /// no "md5" prefix (RFC 014 H3 — one generated credential per endpoint).
    pub encrypted_password: &'a str,
    pub jwks_x_b64url: &'a str,
    pub jwks_kid_b64url: &'a str,
    /// `neon.safekeepers` GUC value, e.g. `safekeeper-0.safekeeper.<ns>.svc.cluster.local:5454`.
    pub safekeepers: &'a str,
    /// `neon.pageserver_connstring` GUC value, e.g. `host=pageserver-0.pageserver.<ns>… port=6400`.
    pub pageserver_connstring: &'a str,
}

/// Runtime settings supplied by the deployment adapter.
pub struct Settings<'a> {
    pub port: u16,
    pub listen_addresses: &'a str,
    pub fsync: bool,
    /// None preserves the engine default; Some("") disables Unix sockets.
    pub unix_socket_directories: Option<&'a str>,
}

/// Render values structurally: quotes, backslashes and placeholder-looking
/// strings in caller input cannot change the JSON document's shape.
pub fn render(p: &SpecParams, settings: &Settings) -> anyhow::Result<Value> {
    p.tenant_id.parse::<TenantId>()?;
    p.timeline_id.parse::<TimelineId>()?;
    if settings.port == 0 || settings.listen_addresses.is_empty() {
        return Err(ValidationError::new(
            "invalid compute listener",
            "supply a nonzero port and listen address",
        )
        .into());
    }
    let mut v: Value = serde_json::from_str(TEMPLATE)?;
    v["spec"]["cluster"]["roles"][0]["encrypted_password"] = json!(p.encrypted_password);
    let key = &mut v["compute_ctl_config"]["jwks"]["keys"][0];
    key["kid"] = json!(p.jwks_kid_b64url);
    key["x"] = json!(p.jwks_x_b64url);
    let gucs = v["spec"]["cluster"]["settings"]
        .as_array_mut()
        .expect("embedded settings array");
    for guc in gucs.iter_mut() {
        let value = match guc["name"].as_str().expect("embedded GUC name") {
            "neon.tenant_id" => p.tenant_id.to_owned(),
            "neon.timeline_id" => p.timeline_id.to_owned(),
            "neon.safekeepers" => p.safekeepers.to_owned(),
            "neon.pageserver_connstring" => p.pageserver_connstring.to_owned(),
            "port" => settings.port.to_string(),
            "listen_addresses" => settings.listen_addresses.to_owned(),
            "fsync" => if settings.fsync { "on" } else { "off" }.to_owned(),
            _ => continue,
        };
        guc["value"] = json!(value);
    }
    if let Some(dirs) = settings.unix_socket_directories {
        gucs.push(json!({"name": "unix_socket_directories", "value": dirs, "vartype": "string"}));
    }
    Ok(v)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn legacy_settings() -> Settings<'static> {
        Settings {
            port: 55433,
            listen_addresses: "0.0.0.0",
            fsync: false,
            unix_socket_directories: None,
        }
    }

    /// The P0 golden: rendered spec == the exact spec a live compute served
    /// data with on kind (tenant dbd27…, spike/NOTES.md Day 3).
    #[test]
    fn golden_matches_p0_spec() {
        let golden: Value =
            serde_json::from_str(include_str!("../tests/fixtures/known-good-spec.json")).unwrap();
        let rendered = render(&SpecParams {
            tenant_id: "dbd271b86f9fa29a8842ac23f67fede5",
            timeline_id: "461d39b24a3592dc712a379c0d3ab6e5",
            // The P0 fixture's credential: md5("sspc-p0" + "cloud_admin").
            encrypted_password: "36817c67283c101851f0ce6de1159c01",
            jwks_x_b64url: "mOLh_FkiKWIGG-AX-yKDcD1KiNvxkk_dcePrTF1GX0c",
            jwks_kid_b64url: "mxha6Szurut0u5Hz0JT058YlUi8tryLtYBBBiUokYtA",
            safekeepers: "safekeeper-0.safekeeper.sspc-cell.svc.cluster.local:5454",
            pageserver_connstring: "host=pageserver-0.pageserver.sspc-cell.svc.cluster.local port=6400",
        }, &legacy_settings())
        .unwrap();
        assert_eq!(rendered, golden);
    }

    #[test]
    fn tenant_and_timeline_land_in_gucs() {
        let v = render(
            &SpecParams {
                tenant_id: "00000000000000000000000000000001",
                timeline_id: "00000000000000000000000000000002",
                encrypted_password: "deadbeef",
                jwks_x_b64url: "x",
                jwks_kid_b64url: "k",
                safekeepers: "sk:5454",
                pageserver_connstring: "host=ps port=6400",
            },
            &legacy_settings(),
        )
        .unwrap();
        let settings = v["spec"]["cluster"]["settings"].as_array().unwrap();
        let get = |name: &str| {
            settings
                .iter()
                .find(|s| s["name"] == name)
                .map(|s| s["value"].as_str().unwrap().to_string())
        };
        assert_eq!(
            get("neon.tenant_id").unwrap(),
            "00000000000000000000000000000001"
        );
        assert_eq!(
            get("neon.timeline_id").unwrap(),
            "00000000000000000000000000000002"
        );
        assert_eq!(v["compute_ctl_config"]["jwks"]["keys"][0]["x"], "x");
    }
}
