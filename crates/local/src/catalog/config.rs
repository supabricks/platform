use super::*;
use std::io::Read;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "mode", rename_all = "snake_case", deny_unknown_fields)]
pub enum Provider {
    Local {
        runtime: Option<PathBuf>,
    },
    External {
        endpoint: String,
        token_file: PathBuf,
        ca_file: Option<PathBuf>,
        metastore_id: String,
    },
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub version: u32,
    pub provider_id: String,
    pub provider: Provider,
}

// Independent of catalog/ so losing the metastore cannot silently replace it.
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LocalIdentity {
    pub version: u32,
    pub provider_id: String,
    pub metastore_id: Option<String>,
}

pub fn local_identity(store: &Store) -> Result<LocalIdentity> {
    let identity: LocalIdentity = serde_json::from_slice(&private_bytes(
        &store.root().join("catalog-local.json"),
        4096,
    )?)?;
    if identity.version != 1
        || identity
            .provider_id
            .parse::<supabricks_core::resource::ProjectId>()
            .is_err()
        || identity
            .metastore_id
            .as_ref()
            .is_some_and(|id| id.parse::<supabricks_core::resource::ProjectId>().is_err())
    {
        return Err(invalid("invalid local catalog identity"));
    }
    Ok(identity)
}

pub fn directory(path: &Path) -> Result<()> {
    if !path.try_exists()? {
        fs::DirBuilder::new().mode(0o700).create(path)?;
    }
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err(conflict(
            "catalog directory must be private, owned and not a symlink",
        ));
    }
    Ok(())
}

pub fn private_bytes(path: &Path, max: u64) -> Result<Vec<u8>> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    let meta = file.metadata()?;
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.mode() & 0o077 != 0
        || meta.len() > max
    {
        return Err(invalid(
            "catalog secret/configuration must be a bounded private owned file",
        ));
    }
    let mut bytes = Vec::new();
    file.take(max + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > max {
        return Err(invalid("catalog file exceeds its bound"));
    }
    Ok(bytes)
}

pub fn token(path: &Path) -> Result<String> {
    let token = String::from_utf8(private_bytes(path, 16384)?)
        .map_err(|_| invalid("invalid catalog token file"))?;
    let token = token.trim();
    if token.len() < 20
        || token
            .bytes()
            .any(|b| b.is_ascii_whitespace() || b.is_ascii_control())
    {
        return Err(invalid("invalid catalog token file"));
    }
    Ok(token.to_owned())
}

pub fn certificates(path: &Path) -> Result<Vec<ureq::tls::Certificate<'static>>> {
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)?;
    if !file.metadata()?.is_file() {
        return Err(invalid("catalog CA must be a regular PEM file"));
    }
    let mut bytes = Vec::new();
    file.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
    if bytes.len() > 1024 * 1024 {
        return Err(invalid("catalog CA file exceeds its bound"));
    }
    let mut certs = Vec::new();
    for item in ureq::tls::parse_pem(&bytes) {
        match item {
            Ok(ureq::tls::PemItem::Certificate(cert)) => certs.push(cert),
            _ => return Err(invalid("catalog CA file must contain only certificates")),
        }
    }
    if certs.is_empty() {
        return Err(invalid("catalog CA file has no certificates"));
    }
    Ok(certs)
}

pub fn validate(provider: &Provider) -> Result<()> {
    match provider {
        Provider::Local { runtime } => {
            if let Some(runtime) = runtime {
                if crate::installation::Installation::discover()?.is_some()
                    || !runtime.is_absolute()
                {
                    return Err(invalid(
                        "catalog runtime overrides require an absolute path in a source build",
                    ));
                }
            }
        }
        Provider::External {
            endpoint,
            token_file,
            ca_file,
            metastore_id,
        } => {
            let uri: ureq::http::Uri = endpoint
                .parse()
                .map_err(|_| invalid("invalid OSS UC endpoint"))?;
            let authority = uri
                .authority()
                .ok_or_else(|| invalid("OSS UC endpoint requires an authority"))?;
            let loopback = uri.host().is_some_and(|h| {
                h == "localhost"
                    || h.trim_matches(['[', ']'])
                        .parse::<std::net::IpAddr>()
                        .is_ok_and(|ip| ip.is_loopback())
            });
            if authority.as_str().contains('@')
                || !matches!(uri.path(), "" | "/")
                || uri.query().is_some()
                || endpoint.contains('#')
                || endpoint.len() > 2048
                || !(uri.scheme_str() == Some("https")
                    || (uri.scheme_str() == Some("http") && loopback))
            {
                return Err(invalid(
                    "OSS UC endpoint must be an HTTPS origin (HTTP is allowed only on loopback), without credentials, path or query",
                ));
            }
            if !token_file.is_absolute()
                || ca_file.as_ref().is_some_and(|p| !p.is_absolute())
                || metastore_id
                    .parse::<supabricks_core::resource::ProjectId>()
                    .is_err()
            {
                return Err(invalid(
                    "external UC requires absolute secret/CA references and an expected metastore UUID",
                ));
            }
            token(token_file)?;
            if let Some(ca) = ca_file {
                certificates(ca)?;
            }
        }
    }
    Ok(())
}

pub fn load(store: &Store) -> Result<Option<Config>> {
    let path = store.root().join("catalog-provider.json");
    if path.try_exists()? {
        let config: Config = serde_json::from_slice(&private_bytes(&path, 32768)?)?;
        if config.version != 1
            || config
                .provider_id
                .parse::<supabricks_core::resource::ProjectId>()
                .is_err()
        {
            return Err(invalid("unsupported catalog provider configuration"));
        }
        validate(&config.provider)?;
        if matches!(config.provider, Provider::Local { .. })
            && local_identity(store)?.provider_id != config.provider_id
        {
            return Err(conflict("local catalog provider identity changed"));
        }
        return Ok(Some(config));
    }
    if crate::installation::Installation::discover()?
        .is_some_and(|i| i.manifest.provenance.get("unity_catalog").is_some())
    {
        let config = new(store, Provider::Local { runtime: None })?;
        supervisor::write_json(&path, &config)?;
        return Ok(Some(config));
    }
    Ok(None)
}

pub fn new(store: &Store, provider: Provider) -> Result<Config> {
    validate(&provider)?;
    let provider_id = match &provider {
        Provider::Local { .. } => {
            let identity = store.root().join("catalog-local.json");
            if !identity.try_exists()? {
                // Existing provider state must never be reset through reconfiguration.
                let configured_local = if store.root().join("catalog-provider.json").try_exists()? {
                    let saved: Config = serde_json::from_slice(&private_bytes(
                        &store.root().join("catalog-provider.json"),
                        32768,
                    )?)?;
                    matches!(saved.provider, Provider::Local { .. })
                } else {
                    false
                };
                if store.root().join("catalog").try_exists()? || configured_local {
                    return Err(conflict(
                        "local catalog identity missing; restore control state",
                    ));
                }
                supervisor::write_json(
                    &identity,
                    &LocalIdentity {
                        version: 1,
                        provider_id: supabricks_core::resource::ProjectId::new().to_string(),
                        metastore_id: None,
                    },
                )?;
            }
            local_identity(store)?.provider_id
        }
        Provider::External { .. } => supabricks_core::resource::ProjectId::new().to_string(),
    };
    Ok(Config {
        version: 1,
        provider_id,
        provider,
    })
}
