//! Selected-prefix verification for v2 shared Delta roots. V1's exact verifier is unchanged.
use crate::store::{Result, error::invalid};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::{Component, Path, PathBuf},
};
use supabricks_core::resource::OperationId;
pub(crate) fn data_root(installation: &Path, d: &Value) -> Result<PathBuf> {
    let id: OperationId = d["manifest"]["capture_identity"]["generation"]
        .as_str()
        .ok_or_else(|| invalid("missing capture identity"))?
        .parse()
        .map_err(|_| invalid("invalid capture identity"))?;
    let storage = d["manifest"]["storage_generation"]
        .as_str()
        .map(|value| {
            value
                .parse::<OperationId>()
                .map_err(|_| invalid("invalid storage generation"))
        })
        .transpose()?
        .unwrap_or(id);
    if !d["manifest"]["storage_generation"].is_null()
        && !d["manifest"]["storage_generation"].is_string()
    {
        return Err(invalid("invalid storage generation"));
    }
    let expected = format!("analytics/incremental/{storage}");
    if d["format_version"] != 2
        || d["manifest"]["format_version"] != 2
        || d["generation"] != expected
    {
        return Err(invalid("invalid incremental descriptor identity"));
    }
    Ok(installation.join(expected))
}
pub(crate) fn layout(installation: &Path, d: &Value) -> Result<Vec<(String, u64, String)>> {
    let root = data_root(installation, d)?;
    if !fs::symlink_metadata(&root)?.is_dir() {
        return Err(invalid("invalid incremental root"));
    }
    let tables = d["manifest"]["tables"]
        .as_array()
        .ok_or_else(|| invalid("missing v2 table map"))?;
    if tables.is_empty() || tables.len() > 128 {
        return Err(invalid("v2 table budget"));
    }
    let mut versions = BTreeMap::new();
    for table in tables {
        let oid = table["oid"]
            .as_u64()
            .ok_or_else(|| invalid("invalid OID"))?;
        let version = table["version"]
            .as_u64()
            .filter(|v| *v < 1024)
            .ok_or_else(|| invalid("invalid Delta version"))?;
        if table["path"] != format!("tables/{oid}")
            || versions.insert(oid.to_string(), version).is_some()
        {
            return Err(invalid("invalid v2 table path or duplicate OID"));
        }
    }
    let files = d["manifest"]["files"]
        .as_array()
        .filter(|f| f.len() <= 4096)
        .ok_or_else(|| invalid("v2 file budget"))?;
    let mut seen = BTreeSet::new();
    let mut checks = Vec::new();
    let mut total = 0u64;
    for file in files {
        let name = file["path"]
            .as_str()
            .ok_or_else(|| invalid("missing v2 path"))?;
        let path = Path::new(name);
        let parts: Vec<_> = name.split('/').collect();
        if !path.components().all(|c| matches!(c, Component::Normal(_)))
            || !seen.insert(name.to_owned())
            || parts.len() < 3
            || parts[0] != "tables"
            || !versions.contains_key(parts[1])
        {
            return Err(invalid("unsafe v2 file path"));
        }
        if parts.len() == 4 && parts[2] == "_delta_log" {
            let version = parts[3]
                .strip_suffix(".json")
                .filter(|s| s.len() == 20 && s.bytes().all(|c| c.is_ascii_digit()))
                .and_then(|s| s.parse::<u64>().ok())
                .ok_or_else(|| invalid("unsupported v2 log"))?;
            if version > versions[parts[1]] {
                return Err(invalid("v2 inventory includes an unselected log"));
            }
        } else if parts.len() != 3 || !parts[2].ends_with(".parquet") {
            return Err(invalid("unsupported v2 data file"));
        }
        let mut current = root.clone();
        for (i, part) in parts.iter().enumerate() {
            current.push(part);
            let m = fs::symlink_metadata(&current)?;
            if (i + 1 == parts.len() && !m.is_file()) || (i + 1 < parts.len() && !m.is_dir()) {
                return Err(invalid("unsafe v2 file type"));
            }
        }
        let size = file["bytes"]
            .as_u64()
            .ok_or_else(|| invalid("missing v2 size"))?;
        let hash = file["sha256"]
            .as_str()
            .filter(|s| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit()))
            .ok_or_else(|| invalid("missing v2 checksum"))?;
        if fs::metadata(current)?.len() != size {
            return Err(invalid("v2 file size changed"));
        }
        total = total
            .checked_add(size)
            .ok_or_else(|| invalid("v2 size overflow"))?;
        if total > 1024 * 1024 * 1024 {
            return Err(invalid("v2 output budget"));
        }
        checks.push((name.to_owned(), size, hash.to_owned()));
    }
    for (oid, version) in versions {
        for v in 0..=version {
            if !seen.contains(&format!("tables/{oid}/_delta_log/{v:020}.json")) {
                return Err(invalid("v2 log prefix has a gap"));
            }
        }
    }
    Ok(checks)
}
