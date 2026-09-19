//! Read-only NE04 bundle inventory. Runtime uv verification remains authoritative.
use crate::store::{Result, error::invalid};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{Cursor, Read},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Closure {
    pub path: String,
    pub sha256: String,
    pub kernel_contract: String,
    pub wheels: usize,
    pub status: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Bundle {
    version: u32,
    target: String,
    contract: String,
    files: BTreeMap<String, String>,
}
fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
pub fn inspect(
    path: &str,
    bytes: &[u8],
    target: &str,
    manifest: &str,
    lock: &str,
) -> Result<Closure> {
    let bad =
        || invalid("invalid NE04 bundle: target, declarations, inventory, hash or size mismatch");
    let mut zip = zip::ZipArchive::new(Cursor::new(bytes)).map_err(|_| bad())?;
    if zip.len() > 259 {
        return Err(bad());
    }
    let mut names = BTreeSet::new();
    let mut total = 0;
    for i in 0..zip.len() {
        let entry = zip.by_index(i).map_err(|_| bad())?;
        if !names.insert(entry.name().to_owned())
            || entry.is_dir()
            || entry.encrypted()
            || entry
                .unix_mode()
                .is_some_and(|m| m & 0o170000 != 0 && m & 0o170000 != 0o100000)
            || entry.size() > super::source::MAX_BUNDLE
        {
            return Err(bad());
        }
        total += entry.size();
        if total > super::source::MAX_BUNDLE {
            return Err(bad());
        }
    }
    let mut metadata = Vec::new();
    zip.by_name("bundle.json")
        .map_err(|_| bad())?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut metadata)?;
    if metadata.len() > 1024 * 1024 {
        return Err(bad());
    }
    let b: Bundle = serde_json::from_slice(&metadata).map_err(|_| bad())?;
    if b.version != 1
        || b.target != target
        || b.contract.len() != 64
        || !b
            .contract
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
        || b.files.get("pyproject.toml").map(String::as_str) != Some(manifest)
        || b.files.get("uv.lock").map(String::as_str) != Some(lock)
        || names
            != b.files
                .keys()
                .cloned()
                .chain(["bundle.json".into()])
                .collect()
    {
        return Err(bad());
    }
    for (name, expected) in &b.files {
        if name != "pyproject.toml"
            && name != "uv.lock"
            && !name.strip_prefix("wheels/").is_some_and(|n| {
                n.ends_with(".whl")
                    && n.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_.+-".contains(&c))
            })
        {
            return Err(bad());
        }
        let mut entry = zip.by_name(name).map_err(|_| bad())?;
        let mut sha = Sha256::new();
        let mut buf = [0; 65536];
        loop {
            let n = entry.read(&mut buf).map_err(|_| bad())?;
            if n == 0 {
                break;
            }
            sha.update(&buf[..n]);
        }
        if hex::encode(sha.finalize()) != *expected {
            return Err(bad());
        }
    }
    Ok(Closure {
        path: path.into(),
        sha256: hash(bytes),
        kernel_contract: b.contract,
        wheels: b.files.len().saturating_sub(2),
        status: "inventory_verified_runtime_preparation_required".into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    fn bundle(target: &str, alter: bool) -> Vec<u8> {
        let mut zip = zip::ZipWriter::new(Cursor::new(Vec::new()));
        let input = b"declaration";
        let mut files = BTreeMap::from([
            ("pyproject.toml", hash(input)),
            ("uv.lock", hash(input)),
            ("wheels/example-1-py3-none-any.whl", hash(input)),
        ]);
        if alter {
            files.insert("uv.lock", "0".repeat(64));
        }
        for name in files.keys() {
            zip.start_file(*name, zip::write::SimpleFileOptions::default())
                .unwrap();
            zip.write_all(input).unwrap();
        }
        zip.start_file("bundle.json", zip::write::SimpleFileOptions::default())
            .unwrap();
        zip.write_all(&serde_json::to_vec(&serde_json::json!({"version":1,"target":target,"contract":"a".repeat(64),"files":files})).unwrap()).unwrap();
        zip.finish().unwrap().into_inner()
    }
    #[test]
    fn bundle_inventory_binds_target_pair_and_every_artifact() {
        let input = hash(b"declaration");
        let bytes = bundle("linux-x86_64", false);
        let closure = inspect(
            "dependencies/linux.zip",
            &bytes,
            "linux-x86_64",
            &input,
            &input,
        )
        .unwrap();
        assert_eq!(closure.wheels, 1);
        assert!(
            inspect(
                "dependencies/linux.zip",
                &bytes,
                "macos-arm64",
                &input,
                &input
            )
            .is_err()
        );
        assert!(
            inspect(
                "dependencies/linux.zip",
                &bytes,
                "linux-x86_64",
                &"0".repeat(64),
                &input
            )
            .is_err()
        );
        assert!(
            inspect(
                "dependencies/linux.zip",
                &bundle("linux-x86_64", true),
                "linux-x86_64",
                &input,
                &"0".repeat(64)
            )
            .is_err()
        );
        assert!(
            inspect(
                "dependencies/linux.zip",
                &bytes[..bytes.len() - 8],
                "linux-x86_64",
                &input,
                &input
            )
            .is_err()
        );
    }
}
