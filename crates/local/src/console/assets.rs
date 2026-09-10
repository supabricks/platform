use crate::{
    installation::Installation,
    store::{Result, error::invalid},
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs,
    io::Read,
    path::{Component, Path, PathBuf},
};

pub const VERSION: u32 = 1;
const MAX_BYTES: u64 = 20 * 1024 * 1024;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub api_version: u32,
    pub files: BTreeMap<String, String>,
}
pub struct Assets {
    pub files: BTreeMap<String, (String, Vec<u8>)>,
}

pub fn discover() -> Result<PathBuf> {
    if let Some(i) = Installation::discover()? {
        return Ok(i.root.join("share/console"));
    }
    Ok(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../console/dist"))
}

impl Assets {
    pub fn load(root: &Path) -> Result<Self> {
        if !fs::symlink_metadata(root).is_ok_and(|m| m.is_dir()) {
            return Err(invalid(
                "console assets missing; source builds: run npm ci --prefix console && npm run build --prefix console; installed releases: reinstall the verified bundle",
            ));
        }
        let mut inventory = BTreeMap::new();
        read_tree(root, root, &mut inventory, &mut 0)?;
        let manifest = inventory.remove("console.json").ok_or_else(|| {
            invalid("console asset manifest missing; rebuild or reinstall the console")
        })?;
        let manifest: Manifest = serde_json::from_slice(&manifest)?;
        if manifest.api_version != VERSION
            || !manifest.files.contains_key("index.html")
            || manifest.files.keys().ne(inventory.keys())
        {
            return Err(invalid(
                "console assets/API version mismatch; rebuild or reinstall the console",
            ));
        }
        let mut files = BTreeMap::new();
        for (name, data) in inventory {
            if !Path::new(&name)
                .components()
                .all(|c| matches!(c, Component::Normal(_)))
                || hex::encode(Sha256::digest(&data)) != manifest.files[&name]
            {
                return Err(invalid(
                    "console asset checksum mismatch; rebuild or reinstall the console",
                ));
            }
            let mime = match Path::new(&name).extension().and_then(|s| s.to_str()) {
                Some("html") => "text/html; charset=utf-8",
                Some("js") => "text/javascript; charset=utf-8",
                Some("css") => "text/css; charset=utf-8",
                Some("svg") => "image/svg+xml",
                Some("woff2") => "font/woff2",
                _ => return Err(invalid("unsupported console asset type")),
            };
            files.insert(format!("/{name}"), (mime.into(), data));
        }
        Ok(Self { files })
    }
}

fn read_tree(
    root: &Path,
    dir: &Path,
    out: &mut BTreeMap<String, Vec<u8>>,
    total: &mut u64,
) -> Result<()> {
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        if path.strip_prefix(root).unwrap().components().count() > 4 || out.len() >= 256 {
            return Err(invalid("console asset inventory exceeds limits"));
        }
        let m = fs::symlink_metadata(&path)?;
        if m.is_dir() {
            read_tree(root, &path, out, total)?;
        } else if m.is_file() {
            let mut data = Vec::new();
            fs::File::open(&path)?
                .take(5 * 1024 * 1024 + 1)
                .read_to_end(&mut data)?;
            *total += data.len() as u64;
            if data.len() > 5 * 1024 * 1024 || *total > MAX_BYTES {
                return Err(invalid("console assets exceed size limits"));
            }
            out.insert(
                path.strip_prefix(root)
                    .unwrap()
                    .to_str()
                    .ok_or_else(|| invalid("console asset path must be UTF-8"))?
                    .to_owned(),
                data,
            );
        } else {
            return Err(invalid("console assets contain a symlink or special file"));
        }
    }
    Ok(())
}
