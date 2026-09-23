//! Immutable catalog views of selected incremental epochs. Never expose a mutable
//! root or historical/orphan parquet files to an isolated workload.
use crate::{
    execution::files,
    store::{Result, error::invalid},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
};

const MAX_BYTES: u64 = crate::execution::MAX_BYTES;
const MAX_LOG_BYTES: u64 = 2 * 1024 * 1024;
fn denied() -> crate::store::Error {
    invalid("incremental epoch view is unavailable or unsupported")
}
fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn data_name(v: &Value) -> Result<&str> {
    v.as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() < 200
                && s.ends_with(".parquet")
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
                && !s.starts_with('.')
        })
        .ok_or_else(denied)
}
fn read_log(root: &Path, entry: &Value) -> Result<Vec<u8>> {
    let size = entry["bytes"]
        .as_u64()
        .filter(|n| *n <= MAX_LOG_BYTES)
        .ok_or_else(denied)?;
    let mut file = files::open(root, entry["path"].as_str().ok_or_else(denied)?)?;
    if file.metadata()?.len() != size {
        return Err(denied());
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(MAX_LOG_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 != size || entry["sha256"] != hash(&bytes) {
        return Err(denied());
    }
    Ok(bytes)
}
pub(crate) struct View {
    pub root: PathBuf,
    pub descriptor: Value,
    source: PathBuf,
    copies: Vec<(String, Value)>,
    logs: BTreeMap<String, Vec<u8>>,
}
impl View {
    /// Read-only planning; every log is bound to the committed descriptor.
    pub(crate) fn plan(installation: &Path, descriptor: &Value) -> Result<Self> {
        let checks = crate::analytics_v2::layout(installation, descriptor)?;
        let source = crate::analytics_v2::data_root(installation, descriptor)?;
        let artifact: supabricks_core::resource::OperationId = descriptor["export_id"]
            .as_str()
            .ok_or_else(denied)?
            .parse()
            .map_err(|_| denied())?;
        let generation = format!("analytics/generations/{artifact}/shared");
        let root = installation.join(&generation);
        let inventory: BTreeMap<_, _> = checks
            .into_iter()
            .map(|(path, bytes, sha256)| {
                (
                    path.clone(),
                    json!({"path":path,"bytes":bytes,"sha256":sha256}),
                )
            })
            .collect();
        let mut manifest = descriptor["manifest"].clone();
        let mut copies = Vec::new();
        let mut logs = BTreeMap::new();
        let mut output = Vec::new();
        let mut total = 0u64;
        let mut log_budget = 0u64;
        for table in manifest["tables"].as_array_mut().ok_or_else(denied)? {
            let oid = table["oid"].as_u64().ok_or_else(denied)?;
            let version = table["version"].as_u64().ok_or_else(denied)?;
            let mut active = BTreeMap::new();
            let mut metadata = None;
            let mut protocol = None;
            for v in 0..=version {
                let name = format!("tables/{oid}/_delta_log/{v:020}.json");
                let bytes = read_log(&source, inventory.get(&name).ok_or_else(denied)?)?;
                log_budget += bytes.len() as u64;
                if log_budget > 32 * 1024 * 1024 {
                    return Err(denied());
                }
                for line in bytes.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
                    let action: Value = serde_json::from_slice(line)?;
                    if action.as_object().is_none_or(|a| a.len() != 1) {
                        return Err(denied());
                    }
                    if let Some(p) = action.get("protocol") {
                        if p["minReaderVersion"] != 1
                            || p["minWriterVersion"] != 2
                            || p.get("readerFeatures").is_some()
                            || p.get("writerFeatures").is_some()
                            || protocol.as_ref().is_some_and(|old| old != p)
                        {
                            return Err(denied());
                        }
                        protocol = Some(p.clone());
                    } else if let Some(m) = action.get("metaData") {
                        if m["format"]["provider"] != "parquet"
                            || m["format"]["options"]
                                .as_object()
                                .is_none_or(|o| !o.is_empty())
                            || m["partitionColumns"]
                                .as_array()
                                .is_none_or(|a| !a.is_empty())
                            || m["configuration"].as_object().is_none_or(|o| {
                                o.iter().any(|(k, v)| {
                                    k != "delta.dataSkippingNumIndexedCols" || v != "0"
                                })
                            })
                            || metadata.as_ref().is_some_and(|old| old != m)
                        {
                            return Err(denied());
                        }
                        metadata = Some(m.clone());
                    } else if let Some(add) = action.get("add") {
                        let name = data_name(&add["path"])?;
                        if add.get("deletionVector").is_some()
                            || add["partitionValues"]
                                .as_object()
                                .is_none_or(|o| !o.is_empty())
                            || active
                                .insert(name.to_owned(), add["size"].as_u64().ok_or_else(denied)?)
                                .is_some()
                        {
                            return Err(denied());
                        }
                    } else if let Some(remove) = action.get("remove") {
                        if remove.get("deletionVector").is_some()
                            || active.remove(data_name(&remove["path"])?).is_none()
                        {
                            return Err(denied());
                        }
                    } else if action.get("commitInfo").is_none() {
                        return Err(denied());
                    }
                }
            }
            let mut actions = vec![
                json!({"protocol":protocol.ok_or_else(denied)?}),
                json!({"metaData":metadata.ok_or_else(denied)?}),
            ];
            for (name, size) in active {
                let from = format!("tables/{oid}/{name}");
                let original = inventory.get(&from).ok_or_else(denied)?;
                if original["bytes"] != size {
                    return Err(denied());
                }
                let mut entry = original.clone();
                entry["path"] = json!(format!("{oid}/{name}"));
                copies.push((from, entry.clone()));
                output.push(entry);
                total = total.checked_add(size).ok_or_else(denied)?;
                actions.push(json!({"add":{"path":name,"size":size,"partitionValues":{},"modificationTime":0,"dataChange":false}}));
            }
            let mut bytes = Vec::new();
            for action in actions {
                serde_json::to_writer(&mut bytes, &action)?;
                bytes.push(b'\n');
            }
            if bytes.len() as u64 > MAX_LOG_BYTES {
                return Err(denied());
            }
            let name = format!("{oid}/_delta_log/00000000000000000000.json");
            total += bytes.len() as u64;
            output.push(json!({"path":name,"bytes":bytes.len(),"sha256":hash(&bytes)}));
            logs.insert(name, bytes);
            table["version"] = json!(0);
            table["path"] = json!(oid.to_string());
        }
        if total > MAX_BYTES || output.len() > 4096 {
            return Err(denied());
        }
        output.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
        manifest["files"] = json!(output);
        manifest["format_version"] = json!(1);
        manifest["generation_bytes"] = json!(total);
        // The view is bound to its source descriptor, including selected versions.
        manifest["view_source_sha256"] = descriptor["manifest_sha256"].clone();
        let mut d = descriptor.clone();
        d["manifest"] = manifest;
        d["manifest_sha256"] = json!(hash(&serde_json::to_vec(&d["manifest"])?));
        d["format_version"] = json!(1);
        d["generation"] = json!(generation);
        Ok(Self {
            root,
            descriptor: d,
            source,
            copies,
            logs,
        })
    }
    pub(crate) fn log(&self, oid: u64) -> Result<&[u8]> {
        self.logs
            .get(&format!("{oid}/_delta_log/00000000000000000000.json"))
            .map(Vec::as_slice)
            .ok_or_else(denied)
    }
    pub(crate) fn verify(&self) -> Result<()> {
        crate::analytics::check_ready(&self.root, &self.descriptor)?;
        for entry in self.descriptor["manifest"]["files"]
            .as_array()
            .ok_or_else(denied)?
        {
            files::verify_file(
                &self.root,
                entry["path"].as_str().ok_or_else(denied)?,
                entry["bytes"].as_u64(),
                entry["sha256"].as_str().ok_or_else(denied)?,
            )?;
        }
        Ok(())
    }
    /// Called only after durable publication admission has pinned the source epoch.
    pub(crate) fn materialize(&self) -> Result<()> {
        if self.root.try_exists()? {
            return self.verify();
        }
        let parent = self.root.parent().ok_or_else(denied)?;
        if parent.canonicalize()? != parent {
            return Err(denied());
        }
        let temp = tempfile::Builder::new()
            .prefix("shared-")
            .tempdir_in(parent)?;
        for (from, entry) in &self.copies {
            let path = entry["path"].as_str().ok_or_else(denied)?;
            let target = temp.path().join(path);
            fs::create_dir_all(target.parent().unwrap())?;
            let mut input = files::open(&self.source, from)?;
            let size = entry["bytes"].as_u64().ok_or_else(denied)?;
            if input.metadata()?.len() != size {
                return Err(denied());
            }
            let mut output = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(target)?;
            if std::io::copy(&mut Read::by_ref(&mut input).take(size + 1), &mut output)? != size {
                return Err(denied());
            }
            output.sync_all()?;
            files::verify_file(
                temp.path(),
                path,
                Some(size),
                entry["sha256"].as_str().ok_or_else(denied)?,
            )?;
        }
        for (name, bytes) in &self.logs {
            let target = temp.path().join(name);
            fs::create_dir_all(target.parent().unwrap())?;
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(target)?;
            file.write_all(bytes)?;
            file.sync_all()?;
        }
        for table in self.descriptor["manifest"]["tables"]
            .as_array()
            .ok_or_else(denied)?
        {
            let path = table["path"].as_str().ok_or_else(denied)?;
            let prefix = format!("{path}/");
            let names: BTreeSet<String> = self.descriptor["manifest"]["files"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|f| f["path"].as_str()?.strip_prefix(&prefix).map(str::to_owned))
                .collect();
            files::validate_delta(&temp.path().join(path), &names)?;
        }
        for (name, value) in [
            ("manifest.json", &self.descriptor["manifest"]),
            ("snapshot.json", &self.descriptor),
        ] {
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(temp.path().join(name))?;
            file.write_all(&serde_json::to_vec(value)?)?;
            file.sync_all()?;
        }
        // Sync child directories before the atomic rename; crash leftovers have no authority.
        let mut dirs = vec![temp.path().to_owned()];
        let mut index = 0;
        while index < dirs.len() {
            for e in fs::read_dir(&dirs[index])? {
                let e = e?;
                if e.file_type()?.is_dir() {
                    dirs.push(e.path());
                }
            }
            index += 1;
        }
        for dir in dirs.iter().rev() {
            fs::File::open(dir)?.sync_all()?;
        }
        fs::rename(temp.path(), &self.root)?;
        fs::File::open(parent)?.sync_all()?;
        self.verify()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (tempfile::TempDir, Value) {
        // macOS temp roots may be reached through /var -> /private/var.
        // Production stores are canonical; fixtures must obey the same boundary.
        let dir = tempfile::tempdir_in(std::env::temp_dir().canonicalize().unwrap()).unwrap();
        let id = supabricks_core::resource::OperationId::new();
        let capture = supabricks_core::resource::OperationId::new();
        let generation = format!("analytics/incremental/{capture}");
        let root = dir.path().join(&generation);
        fs::create_dir_all(root.join("tables/42/_delta_log")).unwrap();
        fs::create_dir_all(dir.path().join(format!("analytics/generations/{id}"))).unwrap();
        let metadata = json!({"metaData":{"id":"table-id","format":{"provider":"parquet","options":{}},"schemaString":"{\"type\":\"struct\",\"fields\":[{\"name\":\"id\",\"type\":\"integer\",\"nullable\":false,\"metadata\":{}}]}","partitionColumns":[],"configuration":{}}});
        let add = |name: &str| json!({"add":{"path":name,"size":3,"partitionValues":{},"stats":"do not expose historical statistics"}});
        let logs = [
            vec![
                json!({"protocol":{"minReaderVersion":1,"minWriterVersion":2}}),
                metadata,
                add("old.parquet"),
            ],
            vec![
                json!({"remove":{"path":"old.parquet"}}),
                add("selected.parquet"),
            ],
            vec![
                json!({"remove":{"path":"selected.parquet"}}),
                add("future.parquet"),
            ],
        ];
        let mut entries = vec![];
        for name in [
            "old.parquet",
            "selected.parquet",
            "future.parquet",
            "orphan.parquet",
        ] {
            let path = format!("tables/42/{name}");
            fs::write(root.join(&path), b"row").unwrap();
            entries.push(json!({"path":path,"bytes":3,"sha256":hash(b"row")}));
        }
        for (version, actions) in logs.iter().enumerate() {
            let bytes = actions.iter().map(|v| format!("{v}\n")).collect::<String>();
            let path = format!("tables/42/_delta_log/{version:020}.json");
            fs::write(root.join(&path), &bytes).unwrap();
            if version <= 1 {
                entries
                    .push(json!({"path":path,"bytes":bytes.len(),"sha256":hash(bytes.as_bytes())}));
            }
        }
        let manifest = json!({"format_version":2,"capture_identity":{"generation":capture},"tables":[{"oid":42,"schema":"public","name":"orders","path":"tables/42","version":1,"rows":1,"columns":[{"name":"id"}]}],"files":entries});
        let descriptor = json!({"format_version":2,"generation":generation,"export_id":id,"manifest_sha256":hash(&serde_json::to_vec(&manifest).unwrap()),"manifest":manifest});
        (dir, descriptor)
    }
    #[test]
    fn selected_epoch_view_excludes_deleted_future_and_orphan_rows() {
        let (dir, d) = fixture();
        let view = View::plan(dir.path(), &d).unwrap();
        assert!(!view.root.exists(), "preview must be read-only");
        view.materialize().unwrap();
        view.materialize().unwrap();
        assert!(view.root.join("42/selected.parquet").exists());
        for name in ["old.parquet", "future.parquet", "orphan.parquet"] {
            assert!(!view.root.join("42").join(name).exists());
        }
        assert!(
            !String::from_utf8(view.log(42).unwrap().to_vec())
                .unwrap()
                .contains("statistics")
        );
        let prepared = crate::execution::Prepared::build(
            dir.path(),
            "sql",
            "select 1",
            vec![crate::execution::Input {
                root: view.root.clone(),
                table: uuid::Uuid::new_v4().to_string(),
                files: view.descriptor["manifest"]["files"]
                    .as_array()
                    .unwrap()
                    .clone(),
            }],
        )
        .unwrap();
        assert!(prepared.dir.path().join("data").exists());
        // Later source commits and files cannot retarget an already admitted view.
        fs::write(view.source.join("tables/42/future.parquet"), b"new").unwrap();
        view.verify().unwrap();
        assert_eq!(
            View::plan(dir.path(), &d).unwrap().descriptor,
            view.descriptor
        );
    }
    #[test]
    fn checksum_gap_symlink_and_unsupported_log_actions_are_closed() {
        let (dir, mut d) = fixture();
        let source = crate::analytics_v2::data_root(dir.path(), &d).unwrap();
        let log = source.join("tables/42/_delta_log/00000000000000000001.json");
        let bytes = fs::read(&log).unwrap();
        fs::write(&log, vec![b' '; bytes.len()]).unwrap();
        assert!(View::plan(dir.path(), &d).is_err());
        fs::write(&log, &bytes).unwrap();
        let view = View::plan(dir.path(), &d).unwrap();
        fs::remove_file(source.join("tables/42/selected.parquet")).unwrap();
        std::os::unix::fs::symlink(
            source.join("tables/42/old.parquet"),
            source.join("tables/42/selected.parquet"),
        )
        .unwrap();
        assert!(view.materialize().is_err());
        assert!(!view.root.exists());
        fs::remove_file(source.join("tables/42/selected.parquet")).unwrap();
        fs::write(source.join("tables/42/selected.parquet"), b"row").unwrap();
        d["manifest"]["files"]
            .as_array_mut()
            .unwrap()
            .retain(|f| f["path"] != "tables/42/_delta_log/00000000000000000000.json");
        assert!(View::plan(dir.path(), &d).is_err());
        let (dir, mut d) = fixture();
        let source = crate::analytics_v2::data_root(dir.path(), &d).unwrap();
        let path = "tables/42/_delta_log/00000000000000000001.json";
        let bytes =
            b"{\"add\":{\"path\":\"../secret.parquet\",\"size\":3,\"partitionValues\":{}}}\n";
        fs::write(source.join(path), bytes).unwrap();
        let entry = d["manifest"]["files"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|f| f["path"] == path)
            .unwrap();
        entry["bytes"] = json!(bytes.len());
        entry["sha256"] = json!(hash(bytes));
        assert!(View::plan(dir.path(), &d).is_err());
    }
}
