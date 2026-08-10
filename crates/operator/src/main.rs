mod crd;
mod keys;
mod lifecycle;
mod mcp;
mod ports;
mod reconcile;
mod spec;
mod storcon;

use std::sync::Arc;

use anyhow::Context;
use k8s_openapi::api::core::v1::Secret;
use kube::api::{Api, PostParams};
use kube::{Client, ResourceExt};
use tracing::info;

use keys::ComputeKey;

const JWK_SECRET: &str = "sspc-compute-jwk";
const MCP_TOKEN_SECRET: &str = "sspc-mcp-token";

/// Load or mint the MCP bearer token (fetch it for `claude mcp add` with:
/// kubectl -n <ns> get secret sspc-mcp-token -o jsonpath='{.data.token}' | base64 -d).
async fn ensure_mcp_token(client: &Client, ns: &str) -> anyhow::Result<String> {
    use aws_lc_rs::rand::SecureRandom;
    let secrets: Api<Secret> = Api::namespaced(client.clone(), ns);
    if let Some(existing) = secrets.get_opt(MCP_TOKEN_SECRET).await? {
        let data = existing
            .data
            .as_ref()
            .and_then(|d| d.get("token"))
            .context("mcp token secret exists but has no token")?;
        return Ok(String::from_utf8(data.0.clone())?);
    }
    let mut raw = [0u8; 24];
    aws_lc_rs::rand::SystemRandom::new()
        .fill(&mut raw)
        .map_err(|e| anyhow::anyhow!("token gen: {e}"))?;
    let token = hex::encode(raw);
    let mut secret = Secret::default();
    secret.metadata.name = Some(MCP_TOKEN_SECRET.into());
    secret.metadata.namespace = Some(ns.into());
    secret.data = Some(
        [("token".to_string(), k8s_openapi::ByteString(token.clone().into_bytes()))].into(),
    );
    secrets.create(&PostParams::default(), &secret).await?;
    info!("generated MCP bearer token in secret {MCP_TOKEN_SECRET}");
    Ok(token)
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

/// Load the compute-auth keypair from its Secret, generating it on first run
/// (RFC 012 D7 — the operator owns the JWKS lifecycle).
async fn ensure_key(client: &Client, ns: &str) -> anyhow::Result<ComputeKey> {
    let secrets: Api<Secret> = Api::namespaced(client.clone(), ns);
    if let Some(existing) = secrets.get_opt(JWK_SECRET).await? {
        let data = existing
            .data
            .as_ref()
            .and_then(|d| d.get("pkcs8.der"))
            .context("jwk secret exists but has no pkcs8.der")?;
        info!("loaded compute JWK from secret {JWK_SECRET}");
        return ComputeKey::from_pkcs8(&data.0);
    }
    let key = ComputeKey::generate()?;
    let mut secret = Secret::default();
    secret.metadata.name = Some(JWK_SECRET.into());
    secret.metadata.namespace = Some(ns.into());
    secret.data = Some(
        [(
            "pkcs8.der".to_string(),
            k8s_openapi::ByteString(key.pkcs8().to_vec()),
        )]
        .into(),
    );
    secrets.create(&PostParams::default(), &secret).await?;
    info!("generated compute JWK and stored in secret {}", secret.name_any());
    Ok(key)
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Both aws-lc-rs and ring can end up in the dep tree; pin the process
    // provider explicitly (aws-lc-rs: the 006-O7 FIPS-aligned choice).
    rustls::crypto::aws_lc_rs::default_provider()
        .install_default()
        .expect("install rustls crypto provider");

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,kube=warn".into()),
        )
        .init();

    let namespace = env_or("SSPC_NAMESPACE", "sspc-cell");
    let storcon_url = env_or("SSPC_STORCON_URL", "http://storage-controller:1234");
    let compute_image = env_or(
        "SSPC_COMPUTE_IMAGE",
        "ghcr.io/neondatabase/compute-node-v16:latest",
    );
    let image_pull_policy = env_or("SSPC_IMAGE_PULL_POLICY", "Never");
    let safekeepers = env_or(
        "SSPC_SAFEKEEPERS",
        &format!("safekeeper-0.safekeeper.{namespace}.svc.cluster.local:5454"),
    );
    let pageserver_connstring = env_or(
        "SSPC_PAGESERVER_CONNSTRING",
        &format!("host=pageserver-0.pageserver.{namespace}.svc.cluster.local port=6400"),
    );

    let client = Client::try_default().await?;
    let key = ensure_key(&client, &namespace).await?;
    info!(
        "sspc-operator starting: ns={namespace} storcon={storcon_url} kid={}",
        key.kid_b64url
    );

    let ctx = Arc::new(reconcile::Ctx {
        client,
        storcon: storcon::Storcon::new(storcon_url),
        key,
        namespace,
        compute_image,
        image_pull_policy,
        safekeepers,
        pageserver_connstring,
        pg_password: env_or("SSPC_PG_PASSWORD", "sspc-p0"),
    });

    tokio::spawn(lifecycle::run(ctx.clone()));

    // POC default is open mode: install binds ports to loopback, so the
    // network layer guards the surface. Set SSPC_MCP_REQUIRE_TOKEN=true to
    // mint/require the bearer instead (real IAM: RFC 008).
    let token = if env_or("SSPC_MCP_REQUIRE_TOKEN", "false") == "true" {
        Some(ensure_mcp_token(&ctx.client, &ctx.namespace).await?)
    } else {
        info!("MCP auth: open mode (loopback-bound POC posture)");
        None
    };
    let mcp_state = Arc::new(mcp::McpState {
        ctx: ctx.clone(),
        token,
        connect_host: env_or("SSPC_CONNECT_HOST", "localhost"),
    });
    let mcp_addr = env_or("SSPC_MCP_ADDR", "0.0.0.0:8080");
    tokio::spawn(async move {
        if let Err(e) = mcp::serve(mcp_state, &mcp_addr).await {
            tracing::error!("mcp server exited: {e}");
        }
    });

    reconcile::run(ctx).await
}
