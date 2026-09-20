use super::*;
use sha2::{Digest, Sha256};
use std::io::Read;
use std::net::TcpListener;
use std::path::Component;

pub struct Runtime {
    pub root: PathBuf,
    pub classpath: String,
}

pub fn verify(root: &Path) -> Result<Runtime> {
    let root = root.canonicalize()?;
    let report: Value = serde_json::from_slice(&fs::read(root.join("build.json"))?)?;
    let pin: Value = serde_json::from_str(include_str!(
        "../../../../components/unity-catalog-source.lock.json"
    ))?;
    let target = if cfg!(target_os = "macos") {
        "macos-arm64"
    } else {
        "linux-x86_64"
    };
    if report["schema_version"] != 2
        || report["source_commit"] != pin["commit"]
        || report["source_dirty"] != false
        || report["target"] != target
        || report["source_pin_sha256"]
            != hex::encode(Sha256::digest(include_bytes!(
                "../../../../components/unity-catalog-source.lock.json"
            )))
        || report["dependency_lock_sha256"]
            != hex::encode(Sha256::digest(include_bytes!(
                "../../../../components/unity-catalog-maven.lock.json"
            )))
    {
        return Err(invalid(
            "catalog runtime differs from reviewed source/dependency inputs",
        ));
    }
    let files = report["files"]
        .as_object()
        .ok_or_else(|| invalid("catalog inventory missing"))?;
    for required in [
        "java/bin/java",
        "classpath.json",
        "configuration-template/hibernate.properties",
        "licenses/LICENSE",
        "dependencies.json",
    ] {
        if !files.contains_key(required) {
            return Err(invalid("catalog runtime inventory incomplete"));
        }
    }
    fn inventory(root: &Path, dir: &Path, paths: &mut Vec<String>) -> Result<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let kind = entry.file_type()?;
            if kind.is_dir() {
                inventory(root, &entry.path(), paths)?;
            } else if kind.is_file() {
                paths.push(
                    entry
                        .path()
                        .strip_prefix(root)
                        .unwrap()
                        .to_string_lossy()
                        .into_owned(),
                );
            } else {
                return Err(invalid(
                    "catalog runtime contains a symlink or special file",
                ));
            }
        }
        Ok(())
    }
    let mut actual = Vec::new();
    inventory(&root, &root, &mut actual)?;
    actual.retain(|s| s != "build.json");
    actual.sort();
    let mut expected: Vec<_> = files.keys().cloned().collect();
    expected.sort();
    if actual != expected {
        return Err(invalid("catalog runtime inventory changed"));
    }
    for (name, checksum) in files {
        if !Path::new(name)
            .components()
            .all(|p| matches!(p, Component::Normal(_)))
        {
            return Err(invalid("invalid catalog inventory path"));
        }
        let mut file = fs::File::open(root.join(name))?;
        let mut hash = Sha256::new();
        let mut buf = [0; 65536];
        loop {
            let n = file.read(&mut buf)?;
            if n == 0 {
                break;
            }
            hash.update(&buf[..n]);
        }
        if checksum.as_str() != Some(hex::encode(hash.finalize()).as_str()) {
            return Err(invalid("catalog runtime checksum mismatch"));
        }
    }
    let entries: Vec<String> = serde_json::from_slice(&fs::read(root.join("classpath.json"))?)?;
    if entries.is_empty()
        || entries
            .iter()
            .any(|p| !p.starts_with("jars/") || !files.contains_key(p))
    {
        return Err(invalid("invalid catalog classpath"));
    }
    let classpath = entries
        .iter()
        .map(|p| root.join(p).to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join(":");
    Ok(Runtime { root, classpath })
}

pub fn resolve(provider: &Provider) -> Result<Runtime> {
    match provider {
        Provider::Local {
            runtime: Some(root),
        } => verify(root),
        Provider::Local { runtime: None } => {
            let installed = crate::installation::Installation::discover()?.ok_or_else(|| {
                invalid("catalog needs an installed runtime or an explicit development runtime")
            })?;
            if installed.manifest.provenance.get("unity_catalog").is_none() {
                return Err(invalid("this release does not include Unity Catalog"));
            }
            verify(&installed.root.join("share/unity-catalog"))
        }
        _ => Err(invalid("external catalog has no managed runtime")),
    }
}

pub fn prepare(store: &Store, runtime: &Runtime) -> Result<PathBuf> {
    let root = store.root().join("catalog");
    config::directory(&root)?;
    for name in ["etc", "etc/conf", "etc/db", "etc/logs", "tmp"] {
        config::directory(&root.join(name))?;
    }
    let conf = root.join("etc/conf");
    // Fail closed on incomplete key state after first successful bootstrap.
    // Explicit key rotation removes the sentinel while stopped.
    if root.join("bootstrapped.json").try_exists()? {
        for name in ["public_key.der", "private_key.der", "key_id.txt"] {
            config::private_bytes(&conf.join(name), 16384)?;
        }
    }
    supervisor::write_private(
        &conf.join("hibernate.properties"),
        &fs::read(
            runtime
                .root
                .join("configuration-template/hibernate.properties"),
        )?,
    )?;
    supervisor::write_private(&conf.join("server.properties"), b"server.env=prod\nserver.authorization=enable\nserver.allowed-issuers=internal\nserver.audiences=supabricks-local-owner\nserver.access-token-timeout=PT5M\n")?;
    supervisor::write_private(&conf.join("server.log4j2.properties"), b"status=warn\nappenders=rolling\nappender.rolling.type=RollingFile\nappender.rolling.name=Rolling\nappender.rolling.fileName=etc/logs/server.log\nappender.rolling.filePattern=etc/logs/server-%i.log.gz\nappender.rolling.layout.type=PatternLayout\nappender.rolling.layout.pattern=%d{ISO8601} %-5p %c{1}: %m%n\nappender.rolling.policies.type=Policies\nappender.rolling.policies.size.type=SizeBasedTriggeringPolicy\nappender.rolling.policies.size.size=5MB\nappender.rolling.strategy.type=DefaultRolloverStrategy\nappender.rolling.strategy.max=3\nrootLogger.level=warn\nrootLogger.appenderRefs=rolling\nrootLogger.appenderRef.rolling.ref=Rolling\n")?;
    Ok(root)
}

pub fn ports(store: &Store) -> Result<(TcpListener, TcpListener)> {
    let reserved: Vec<u16> = store
        .branches()?
        .into_iter()
        .filter_map(|b| b.ports)
        .flat_map(|p| [p.sql, p.external_http, p.internal_http])
        .chain(store.connection_ports()?.into_iter().map(|(_, p)| p))
        .collect();
    for _ in 0..100 {
        let first = TcpListener::bind("127.0.0.1:0")?;
        let port = first.local_addr()?.port();
        if port == u16::MAX || reserved.contains(&port) || reserved.contains(&(port + 1)) {
            continue;
        }
        if let Ok(second) = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, port + 1)) {
            return Ok((first, second));
        }
    }
    Err(conflict(
        "catalog could not reserve adjacent loopback ports",
    ))
}

pub fn rotate_log(path: &Path) -> Result<()> {
    if fs::symlink_metadata(path).is_ok_and(|m| !m.is_file()) {
        return Err(conflict("invalid catalog log file"));
    }
    if path.metadata().is_ok_and(|m| m.len() >= 5 * 1024 * 1024) {
        // stdout's open descriptor must keep the same inode while UC runs.
        // Keep one bounded previous tail and truncate the active file in place.
        use std::io::{Seek, SeekFrom};
        let mut file = fs::File::open(path)?;
        file.seek(SeekFrom::End(-(5 * 1024 * 1024)))?;
        let mut tail = Vec::new();
        file.take(5 * 1024 * 1024).read_to_end(&mut tail)?;
        supervisor::write_private(&path.with_extension("log.1"), &tail)?;
        crate::store::ownership::private_file(path)?.set_len(0)?;
    }
    Ok(())
}
