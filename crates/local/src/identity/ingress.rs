//! Explicit operator-owned TLS ingress. Only the governed adapter is reachable.
use super::*;
use serde::Deserialize;
use std::{
    fs,
    io::BufReader,
    net::SocketAddr,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::io::AsyncReadExt;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen: SocketAddr,
    pub origin: String,
    pub certificate: PathBuf,
    pub private_key: PathBuf,
    pub qualification: Option<PathBuf>,
}

impl Config {
    pub fn load(path: &Path, redirect: &str) -> Result<Self> {
        let value = super::transport::read_private(path)?;
        let config: Self = serde_json::from_value(value)?;
        config.validate(redirect)?;
        Ok(config)
    }

    fn validate(&self, redirect: &str) -> Result<()> {
        if !cfg!(all(target_os = "linux", target_arch = "x86_64")) {
            return Err(denied());
        }
        let url = reqwest::Url::parse(&self.origin).map_err(|_| denied())?;
        if url.scheme() != "https"
            || url.origin().ascii_serialization() != self.origin
            || url.host_str().is_none()
            || self.listen.port() == 0
            || url.port_or_known_default() != Some(self.listen.port())
            || redirect != format!("{}/auth/v1/callback", self.origin)
        {
            return Err(denied());
        }
        let installed = crate::installation::Installation::discover()?.ok_or_else(denied)?;
        installed.verify()?;
        if let Some(path) = &self.qualification {
            // This is an operator-reviewed receipt, not an authentication credential.
            // It must have been emitted by the complete R04 collector for this archive.
            let receipt = super::transport::read_private(path)?;
            if receipt["schema_version"] != 1
                || receipt["status"] != "passed"
                || receipt["profile"] != "linux-governed-shared-v1"
                || receipt["release_identity"] != installed.identity
                || receipt["target"] != "linux-x86_64"
                || receipt["r04_sha256"]
                    .as_str()
                    .is_none_or(|s| s.len() != 64 || !s.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return Err(denied());
            }
        } else if !self.listen.ip().is_loopback()
            || !matches!(url.host_str(), Some("127.0.0.1" | "[::1]"))
        {
            // A candidate can be qualified over TLS before its full release passes.
            // Network-facing use requires the resulting exact-archive receipt.
            return Err(denied());
        }
        Ok(())
    }

    pub(crate) fn start(&self, root: &Path) -> Result<tiny_http::Server> {
        let metadata = fs::symlink_metadata(root)?;
        if !metadata.is_dir()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
        {
            return Err(denied());
        }
        let certificate = crate::catalog::config::private_bytes(&self.certificate, 65536)?;
        let private_key = crate::catalog::config::private_bytes(&self.private_key, 16384)?;
        let certificates = rustls_pemfile::certs(&mut BufReader::new(certificate.as_slice()))
            .collect::<std::io::Result<Vec<_>>>()?;
        let key = rustls_pemfile::private_key(&mut BufReader::new(private_key.as_slice()))?
            .ok_or_else(denied)?;
        let tls = rustls::ServerConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .map_err(|_| denied())?
        .with_no_client_auth()
        .with_single_cert(certificates, key)
        .map_err(|_| denied())?;
        let socket = root.join("governed-console.sock");
        if socket.exists() {
            use std::os::unix::fs::FileTypeExt;
            let meta = fs::symlink_metadata(&socket)?;
            if !meta.file_type().is_socket()
                || meta.uid() != unsafe { libc::geteuid() }
                || std::os::unix::net::UnixStream::connect(&socket).is_ok()
            {
                return Err(denied());
            }
            fs::remove_file(&socket)?;
        }
        let listener = std::net::TcpListener::bind(self.listen)?;
        listener.set_nonblocking(true)?;
        let server = tiny_http::Server::http_unix(&socket).map_err(|_| denied())?;
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()?;
        std::thread::spawn(move || {
            runtime.block_on(async move {
                let Ok(listener) = tokio::net::TcpListener::from_std(listener) else {
                    return;
                };
                let tls = tokio_rustls::TlsAcceptor::from(Arc::new(tls));
                let slots = Arc::new(tokio::sync::Semaphore::new(32));
                while let Ok((connection, _)) = listener.accept().await {
                    let Ok(slot) = slots.clone().try_acquire_owned() else {
                        continue;
                    };
                    let tls = tls.clone();
                    let socket = socket.clone();
                    tokio::spawn(async move {
                        let _slot = slot;
                        let _ = tokio::time::timeout(Duration::from_secs(120), async move {
                            let Ok(Ok(stream)) = tokio::time::timeout(
                                Duration::from_secs(5),
                                tls.accept(connection),
                            )
                            .await
                            else {
                                return;
                            };
                            let Ok(upstream) = tokio::net::UnixStream::connect(socket).await else {
                                return;
                            };
                            let (reader, mut writer) = tokio::io::split(stream);
                            let (mut response, mut request) = upstream.into_split();
                            // Bound connection input, output and lifetime independently of
                            // application command limits; no forwarding headers are trusted.
                            let mut input = reader.take(1024 * 1024);
                            let mut output = (&mut response).take(32 * 1024 * 1024);
                            tokio::select! {
                                _ = tokio::io::copy(&mut input,&mut request) => {},
                                _ = tokio::io::copy(&mut output,&mut writer) => {},
                            }
                        })
                        .await;
                    });
                }
            })
        });
        Ok(server)
    }
}
