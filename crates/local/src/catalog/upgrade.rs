//! One named UC09 backend transition. JRE, H2 and every dependency stay exact.
use super::*;
use crate::installation::Installation;
use crate::store::error::conflict;

const PREDECESSOR: &str = "8e195426ce03e593b03c92f87051d7bf013aeee1";
const SUCCESSOR: &str = "17280eababcc4ae31c53522d930ef2fe92d1c331";
const SERVER: &str = "share/unity-catalog/jars/258-unitycatalog-server-0.6.0.jar";

pub(crate) fn supported(old: &Installation, new: &Installation) -> Result<bool> {
    if old.manifest.version != "v0.1.0-alpha.34"
        || old.manifest.provenance["unity_catalog"]["source_commit"] != PREDECESSOR
        || new.manifest.provenance["unity_catalog"]["source_commit"] != SUCCESSOR
    {
        return Ok(false);
    }
    let stable = |installation: &Installation| {
        installation
            .manifest
            .files
            .iter()
            .filter(|(name, _)| {
                name.starts_with("share/unity-catalog/")
                    && *name != SERVER
                    && *name != "share/unity-catalog/build.json"
            })
            .map(|(name, file)| (name.clone(), (file.sha256.clone(), file.executable)))
            .collect::<std::collections::BTreeMap<_, _>>()
    };
    if stable(old).is_empty()
        || stable(old) != stable(new)
        || !old.manifest.files.contains_key(SERVER)
        || !new.manifest.files.contains_key(SERVER)
    {
        return Err(conflict(
            "UC09 transition requires identical JRE, H2, dependency and configuration inventories",
        ));
    }
    Ok(true)
}

pub(crate) fn preflight(root: &Path, previous: &Path, pending: bool) -> Result<()> {
    let path = root.join("catalog-format.json");
    if path.exists() {
        let mut source = super::recovery::contract();
        source["server_commit"] = json!(PREDECESSOR);
        let actual: Value = serde_json::from_slice(&config::private_bytes(&path, 16384)?)?;
        if actual != source && !(pending && actual == super::recovery::contract()) {
            return Err(conflict("UC09 transition source backend contract differs"));
        }
    }
    let path = root.join("catalog-provider.json");
    if path.exists() {
        let cfg: config::Config = serde_json::from_slice(&config::private_bytes(&path, 16384)?)?;
        match cfg.provider {
            Provider::Local { runtime: None } => {}
            Provider::Local {
                runtime: Some(path),
            } if path.canonicalize()? == previous.join("share/unity-catalog") => {}
            _ => {
                return Err(conflict(
                    "UC09 transition supports only the previous installed managed catalog",
                ));
            }
        }
    }
    Ok(())
}

pub(crate) fn finish(root: &Path, db: &rusqlite::Connection) -> Result<()> {
    let path = root.join("catalog-provider.json");
    if path.exists() {
        let mut cfg: config::Config =
            serde_json::from_slice(&config::private_bytes(&path, 16384)?)?;
        if !matches!(cfg.provider, Provider::Local { .. }) {
            return Err(conflict("managed catalog transition required"));
        }
        cfg.provider = Provider::Local { runtime: None };
        crate::recovery::atomic_json(&path, &serde_json::to_value(cfg)?)?;
        crate::recovery::atomic_json(
            &root.join("catalog-format.json"),
            &super::recovery::contract(),
        )?;
        // Current pinned JRE/H2 validates the stopped metastore and every retained
        // publication before the runtime/current link can be committed.
        super::recovery::checkpoint(root, db)?;
        if root.join("catalog/etc/db/h2db.mv.db").exists() {
            crate::recovery::atomic_json(
                &root.join("catalog/rotate-key.json"),
                &json!({"version":1}),
            )?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn release(version: &str, commit: &str, server: &str) -> Installation {
        Installation {
            root: PathBuf::from("/installed"), identity: "a".repeat(64),
            manifest:std::sync::Arc::new(serde_json::from_value(json!({
                "format_version":1,"version":version,"target":"linux-x86_64","profile":"local-analytical-preview",
                "provenance":{"unity_catalog":{"source_commit":commit}},
                "files":{SERVER:{"sha256":server,"executable":false},
                    "share/unity-catalog/jars/h2.jar":{"sha256":"exact-h2","executable":false}}
            })).unwrap()),
        }
    }
    #[test]
    fn only_named_server_change_can_cross_the_exact_dependency_boundary() {
        let old = release("v0.1.0-alpha.34", PREDECESSOR, "old");
        let mut new = release("v0.1.0-alpha.35", SUCCESSOR, "new");
        assert!(supported(&old, &new).unwrap());
        std::sync::Arc::get_mut(&mut new.manifest)
            .unwrap()
            .files
            .get_mut("share/unity-catalog/jars/h2.jar")
            .unwrap()
            .sha256 = "changed-h2".into();
        assert!(supported(&old, &new).is_err());
        assert!(!supported(&release("v0.1.0-alpha.34", "unknown", "old"), &new).unwrap());
        assert!(!supported(&release("v0.1.0-alpha.33", PREDECESSOR, "old"), &new).unwrap());
    }
    #[test]
    fn transition_requires_the_exact_source_contract_or_its_own_pending_successor() {
        let temp = tempfile::tempdir().unwrap();
        let mut contract = super::super::recovery::contract();
        contract["server_commit"] = json!(PREDECESSOR);
        crate::recovery::atomic_json(&temp.path().join("catalog-format.json"), &contract).unwrap();
        preflight(temp.path(), Path::new("/installed"), false).unwrap();
        contract["backend_schema"] = json!(2);
        crate::recovery::atomic_json(&temp.path().join("catalog-format.json"), &contract).unwrap();
        assert!(preflight(temp.path(), Path::new("/installed"), true).is_err());
        crate::recovery::atomic_json(
            &temp.path().join("catalog-format.json"),
            &super::super::recovery::contract(),
        )
        .unwrap();
        assert!(preflight(temp.path(), Path::new("/installed"), false).is_err());
        preflight(temp.path(), Path::new("/installed"), true).unwrap();
    }
}
