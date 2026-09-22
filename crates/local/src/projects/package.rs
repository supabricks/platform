//! Source-only .sbproj v1: bounded deterministic tar+gzip; never executes inputs.
use super::{
    Inspection, inspect_inputs,
    source::{MAX_FILES, MAX_TOTAL, Source},
};
use crate::store::{
    Result,
    error::{conflict, invalid},
};
use flate2::{Compression, GzBuilder, bufread::GzDecoder};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::OpenOptions,
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};

const MAX_ARCHIVE: u64 = 300 * 1024 * 1024;
const MAX_METADATA: usize = 2 * 1024 * 1024;
const MAX_RATIO: u64 = 200;
const MARKER: &str = "supabricks-unpacked.json";
const EXCLUSIONS: &[&str] = &[
    "Undeclared files are not read or packaged.",
    "Private state, .env files, keys, connection profiles, caches, virtual environments and runtime binaries are forbidden inputs.",
    "Notebook outputs, execution counts, widget state and runtime metadata are removed from the packaged copy.",
    "Saved queries remain private unless explicitly exported into declared source files.",
    "Arbitrary source and fixture contents are not a complete secret scan.",
];
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Content {
    format_version: u32,
    profile: String,
    inspection: Value,
    exclusions: Vec<String>,
}
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Metadata {
    content: Content,
    content_sha256: String,
}
#[derive(Debug, Serialize)]
pub struct Report {
    pub api_version: u32,
    pub package_format_version: u32,
    pub verified: bool,
    pub archive_sha256: String,
    pub content_sha256: String,
    pub inspection: Inspection,
    pub exclusions: Vec<String>,
}
fn hash(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn canonical(value: &impl Serialize) -> Result<Vec<u8>> {
    let mut value = serde_json::to_value(value)?;
    value.sort_all_objects();
    Ok(serde_json::to_vec(&value)?)
}
fn exclusions() -> Vec<String> {
    EXCLUSIONS.iter().map(|s| (*s).into()).collect()
}

/// Produce a new archive; never overwrite an existing path or modify source files.
pub fn pack(directory: &Path, output: &Path, target: Option<&str>) -> Result<Report> {
    let prepared = prepare(directory, target)?;
    super::publication::write_new(output, &prepared.archive)?;
    Ok(prepared.report)
}
pub(crate) struct Prepared {
    pub report: Report,
    pub source_sha256: String,
    pub archive: Vec<u8>,
}
/// The same deterministic package calculation as pack, without any publication.
pub(crate) fn prepare(directory: &Path, target: Option<&str>) -> Result<Prepared> {
    let mut source = Source::new(directory)?;
    let original = inspect_inputs(&mut source, target)?;
    if original.definition.format_version != 2 {
        return Err(invalid(
            "project pack requires an explicit format-2 source definition; format-1 runtime projects are not implicitly converted",
        ));
    }
    let bundled = source.files.keys().any(|p| super::source::is_bundle(p));
    let mut payload = source.payload.clone();
    for (name, bytes) in &mut payload {
        if name.eq_ignore_ascii_case(MARKER) {
            return Err(invalid(
                "unpack completion marker cannot be a package input",
            ));
        }
        if name.to_ascii_lowercase().ends_with(".ipynb") {
            *bytes = strip_notebook(bytes)?;
        }
        if name.ends_with(".toml") || name.ends_with(".lock") {
            reject_credentials(bytes)?;
        }
    }
    let mut memory = Source::memory(payload.clone())?;
    let inspection = inspect_inputs(&mut memory, Some(&original.target))?;
    let content = Content {
        format_version: 1,
        profile: "source".into(),
        inspection: serde_json::to_value(&inspection)?,
        exclusions: exclusions(),
    };
    let metadata = Metadata {
        content_sha256: hash(&canonical(&content)?),
        content,
    };
    let metadata_bytes = canonical(&metadata)?;
    if metadata_bytes.len() > MAX_METADATA {
        return Err(invalid("package metadata exceeds 2 MiB"));
    }
    let mut entries = BTreeMap::from([("package.json".into(), metadata_bytes)]);
    for (name, bytes) in payload {
        entries.insert(format!("project/{name}"), bytes);
    }
    let raw = tar_bytes(&entries)?;
    // Wheel ZIPs are already compressed; avoid recompressing large closures.
    let mut archive = gzip(
        &raw,
        if bundled {
            Compression::none()
        } else {
            Compression::default()
        },
    )?;
    // Highly compressible legitimate inputs must still satisfy the reader's bomb budget.
    if raw.len() as u64 > archive.len() as u64 * MAX_RATIO {
        archive = gzip(&raw, Compression::none())?;
    }
    if archive.len() as u64 > MAX_ARCHIVE {
        return Err(invalid("compressed package exceeds 300 MiB"));
    }
    // Reopen all source descriptors before publication; fail on observed substitutions.
    source.verify()?;
    Ok(Prepared {
        source_sha256: original.source_sha256,
        report: Report {
            api_version: 1,
            package_format_version: 1,
            verified: true,
            archive_sha256: hash(&archive),
            content_sha256: metadata.content_sha256,
            inspection,
            exclusions: exclusions(),
        },
        archive,
    })
}
pub fn verify(archive: &Path, target: Option<&str>) -> Result<Report> {
    Ok(read(archive, target)?.0)
}
pub fn unpack(archive: &Path, destination: &Path, target: Option<&str>) -> Result<Report> {
    let (report, files) = read(archive, target)?;
    let publication = super::publication::Publication::new(destination)?;
    for (name, bytes) in files {
        publication.write(&name, &bytes)?;
    }
    // This records completion/provenance, never grants a runtime binding or authority.
    publication.write(MARKER, &canonical(&json!({"format_version":1,"state":"unbound","archive_sha256":report.archive_sha256,"content_sha256":report.content_sha256}))?)?;
    publication.publish_directory()?;
    Ok(report)
}
fn gzip(bytes: &[u8], compression: Compression) -> Result<Vec<u8>> {
    let mut encoder = GzBuilder::new()
        .mtime(0)
        .operating_system(255)
        .write(Vec::new(), compression);
    encoder.write_all(bytes)?;
    Ok(encoder.finish()?)
}
fn tar_bytes(files: &BTreeMap<String, Vec<u8>>) -> Result<Vec<u8>> {
    let mut archive = tar::Builder::new(Vec::new());
    for (path, bytes) in files {
        let mut header = tar::Header::new_ustar();
        header.set_path(path).map_err(|_| {
            invalid("package path cannot be represented as USTAR; shorten file or directory names")
        })?;
        header.set_size(bytes.len() as u64);
        header.set_mode(0o600);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        header.set_entry_type(tar::EntryType::Regular);
        header.set_cksum();
        archive.append(&header, bytes.as_slice())?;
    }
    Ok(archive.into_inner()?)
}
pub(crate) fn read(
    path: &Path,
    target: Option<&str>,
) -> Result<(Report, BTreeMap<String, Vec<u8>>)> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)?;
    let before = file.metadata()?;
    if !before.is_file() || before.len() > MAX_ARCHIVE {
        return Err(invalid("package must be a regular file of at most 300 MiB"));
    }
    let mut compressed = Vec::new();
    (&file).take(MAX_ARCHIVE + 1).read_to_end(&mut compressed)?;
    let after = file.metadata()?;
    if compressed.len() as u64 > MAX_ARCHIVE
        || before.len() != after.len()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || before.ctime() != after.ctime()
        || before.ctime_nsec() != after.ctime_nsec()
    {
        return Err(conflict("package changed during read; retry"));
    }
    decode(&compressed, target)
}
fn decode(compressed: &[u8], target: Option<&str>) -> Result<(Report, BTreeMap<String, Vec<u8>>)> {
    if compressed.len() as u64 > MAX_ARCHIVE {
        return Err(invalid("compressed package exceeds limit"));
    }
    // The envelope must not hide filenames, comments, timestamps or extension data.
    if compressed.len() < 10
        || compressed[3] != 0
        || compressed[4..8] != [0; 4]
        || compressed[9] != 255
    {
        return Err(invalid(
            "package gzip header must omit optional metadata and use zero time / OS 255",
        ));
    }
    let limit = MAX_ARCHIVE.min(compressed.len() as u64 * MAX_RATIO);
    let mut decoder = GzDecoder::new(compressed);
    let mut raw = Vec::new();
    (&mut decoder)
        .take(limit + 1)
        .read_to_end(&mut raw)
        .map_err(|_| invalid("invalid or corrupt package gzip stream"))?;
    if raw.len() as u64 > limit {
        return Err(invalid(
            "package exceeds expanded size or 200:1 ratio limit",
        ));
    }
    if !decoder.into_inner().is_empty() {
        return Err(invalid(
            "package has trailing data or multiple gzip members",
        ));
    }
    let mut archive = tar::Archive::new(raw.as_slice());
    let mut entries = BTreeMap::new();
    let mut total = 0u64;
    for entry in archive
        .entries()
        .map_err(|_| invalid("invalid package tar"))?
        .raw(true)
    {
        let mut entry = entry.map_err(|_| invalid("invalid package tar entry"))?;
        if !entry.header().entry_type().is_file() || entry.header().entry_type().as_byte() != b'0' {
            return Err(invalid(
                "package accepts regular USTAR files only; links, extensions and special files are forbidden",
            ));
        }
        let path = std::str::from_utf8(&entry.path_bytes())
            .map_err(|_| invalid("package path must be UTF-8"))?
            .to_owned();
        let limit = if path == "package.json" {
            MAX_METADATA as u64
        } else {
            let name = path
                .strip_prefix("project/")
                .ok_or_else(|| invalid("undeclared package entry"))?;
            super::source::path(name, false)?;
            if name.eq_ignore_ascii_case(MARKER) {
                return Err(invalid("package cannot carry an unpack completion marker"));
            }
            super::source::file_limit(path.strip_prefix("project/").unwrap())
        };
        let size = entry.size();
        if size > limit
            || entries.len() > MAX_FILES
            || total + size > MAX_TOTAL + MAX_METADATA as u64
        {
            return Err(invalid(
                "package entry, count or expanded payload exceeds limits",
            ));
        }
        let mut bytes = Vec::new();
        entry
            .read_to_end(&mut bytes)
            .map_err(|_| invalid("truncated package entry"))?;
        if bytes.len() as u64 != size {
            return Err(invalid("truncated package payload"));
        }
        total += size;
        if entries.insert(path, bytes).is_some() {
            return Err(invalid("duplicate package path"));
        }
    }
    // Enforce order, normalized modes/owners/times, USTAR representation, zero padding
    // and the exact end marker. This also rejects hidden entries after a tar terminator.
    if tar_bytes(&entries)? != raw {
        return Err(invalid("noncanonical or incomplete package tar stream"));
    }
    let metadata_bytes = entries
        .remove("package.json")
        .ok_or_else(|| invalid("missing package.json"))?;
    let metadata: Metadata =
        serde_json::from_slice(&metadata_bytes).map_err(|_| invalid("invalid package metadata"))?;
    if canonical(&metadata)? != metadata_bytes
        || metadata.content.format_version != 1
        || metadata.content.profile != "source"
        || metadata.content.exclusions != exclusions()
    {
        return Err(invalid("unsupported or noncanonical package metadata"));
    }
    if hash(&canonical(&metadata.content)?) != metadata.content_sha256 {
        return Err(invalid("package content digest mismatch"));
    }
    let files: BTreeMap<String, Vec<u8>> = entries
        .into_iter()
        .map(|(p, b)| (p.strip_prefix("project/").unwrap().to_owned(), b))
        .collect();
    let stored_target = metadata.content.inspection["target"]
        .as_str()
        .ok_or_else(|| invalid("missing package target"))?;
    let mut source = Source::memory(files.clone())?;
    let inspection = inspect_inputs(&mut source, Some(stored_target))?;
    if inspection.definition.format_version != 2
        || inspection.files.len() != files.len()
        || serde_json::to_value(&inspection)? != metadata.content.inspection
    {
        return Err(invalid(
            "package inventory, graph or source digest mismatch",
        ));
    }
    for (name, bytes) in &files {
        if name.to_ascii_lowercase().ends_with(".ipynb") && strip_notebook(bytes)? != *bytes {
            return Err(invalid("package contains unstripped notebook state"));
        }
        if name.ends_with(".toml") || name.ends_with(".lock") {
            reject_credentials(bytes)?;
        }
    }
    let inspection = if let Some(target) = target {
        inspect_inputs(&mut source, Some(target))?
    } else {
        inspection
    };
    let report = Report {
        api_version: 1,
        package_format_version: 1,
        verified: true,
        archive_sha256: hash(compressed),
        content_sha256: metadata.content_sha256,
        inspection,
        exclusions: exclusions(),
    };
    Ok((report, files))
}
fn strip_notebook(bytes: &[u8]) -> Result<Vec<u8>> {
    let mut notebook: Value =
        serde_json::from_slice(bytes).map_err(|_| invalid("invalid notebook JSON in package"))?;
    crate::notebooks::files::validate_document(&notebook)?;
    clean_metadata(&mut notebook["metadata"]);
    for cell in notebook["cells"].as_array_mut().unwrap() {
        clean_metadata(&mut cell["metadata"]);
        if cell["cell_type"] == "code" {
            cell["outputs"] = json!([]);
            cell["execution_count"] = Value::Null;
        }
    }
    canonical(&notebook)
}
fn clean_metadata(value: &mut Value) {
    if let Some(metadata) = value.as_object_mut() {
        metadata.retain(|key, _| {
            !matches!(
                key.as_str(),
                "widgets"
                    | "execution"
                    | "executionInfo"
                    | "ExecuteTime"
                    | "trusted"
                    | "collapsed"
                    | "scrolled"
            ) && !key.starts_with("supabricks")
        });
    }
}
fn reject_credentials(bytes: &[u8]) -> Result<()> {
    let value: toml::Value = super::manifest::parse(bytes, "package configuration")?;
    fn check(value: &toml::Value) -> Result<()> {
        match value {
            toml::Value::String(s) => {
                // Covers recognized TOML URI values, including uv source/index URLs.
                if let Some((_, rest)) = s.split_once("://") {
                    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
                    let lower = rest.to_ascii_lowercase();
                    if authority.contains('@')
                        || [
                            "password=",
                            "token=",
                            "secret=",
                            "signature=",
                            "credential=",
                            "api_key=",
                        ]
                        .iter()
                        .any(|key| lower.contains(key))
                    {
                        return Err(invalid(
                            "credential-bearing configuration URI cannot be packaged; use destination credentials",
                        ));
                    }
                }
            }
            toml::Value::Array(a) => {
                for v in a {
                    check(v)?;
                }
            }
            toml::Value::Table(t) => {
                for v in t.values() {
                    check(v)?;
                }
            }
            _ => (),
        }
        Ok(())
    }
    check(&value)
}

#[cfg(test)]
mod tests {
    use super::super::source::MAX_FILE;
    use super::*;
    fn entries() -> BTreeMap<String, Vec<u8>> {
        let t = tempfile::tempdir().unwrap();
        let path = t.path().join("base.sbproj");
        pack(
            &Path::new(env!("CARGO_MANIFEST_DIR")).join("../../examples/projects/sales"),
            &path,
            None,
        )
        .unwrap();
        let compressed = std::fs::read(path).unwrap();
        let mut raw = Vec::new();
        GzDecoder::new(compressed.as_slice())
            .read_to_end(&mut raw)
            .unwrap();
        let mut archive = tar::Archive::new(raw.as_slice());
        archive
            .entries()
            .unwrap()
            .map(|e| {
                let mut e = e.unwrap();
                let p = String::from_utf8(e.path_bytes().into_owned()).unwrap();
                let mut b = Vec::new();
                e.read_to_end(&mut b).unwrap();
                (p, b)
            })
            .collect()
    }
    fn encode(entries: &BTreeMap<String, Vec<u8>>) -> Vec<u8> {
        gzip(&tar_bytes(entries).unwrap(), Compression::default()).unwrap()
    }
    #[test]
    fn corrupt_hashes_extra_payload_and_metadata_extensions_fail() {
        let base = entries();
        assert!(decode(&encode(&base), None).is_ok());
        let mut changed = base.clone();
        changed
            .get_mut("project/queries/sales_total.sql")
            .unwrap()
            .push(b' ');
        assert!(decode(&encode(&changed), None).is_err());
        for path in [
            "project/extra.sql",
            "project/Queries/other.sql",
            "project/notebooks",
            "project/.env",
            "project/supabricks-unpacked.json",
            "dependencies/linux/wheel.whl",
        ] {
            let mut changed = base.clone();
            changed.insert(path.into(), b"private".to_vec());
            assert!(decode(&encode(&changed), None).is_err(), "{path}");
        }
        for change in [
            json!({"hooks":{"install":"run me"}}),
            json!({"grants":[{"principal":"foreign","capability":"read"}]}),
            json!({"effective_principal":"foreign-service"}),
            json!({"identity_sessions":["foreign-token"]}),
            json!({"profile":"offline"}),
            json!({"format_version":2}),
        ] {
            let mut changed = base.clone();
            let mut meta: Value = serde_json::from_slice(&changed["package.json"]).unwrap();
            meta["content"]
                .as_object_mut()
                .unwrap()
                .extend(change.as_object().unwrap().clone());
            meta["content_sha256"] = json!(hash(&canonical(&meta["content"]).unwrap()));
            changed.insert("package.json".into(), canonical(&meta).unwrap());
            assert!(decode(&encode(&changed), None).is_err());
        }
    }
    #[test]
    fn duplicate_traversal_links_special_files_and_tar_trailers_fail() {
        let base = entries();
        let raw = tar_bytes(&base).unwrap();
        for kind in [b'1', b'2', b'3', b'5', b'x', b'L'] {
            let mut changed = raw.clone();
            changed[156] = kind;
            let mut header = tar::Header::from_byte_slice(&changed[..512]).clone();
            header.set_cksum();
            changed[..512].copy_from_slice(header.as_bytes());
            assert!(decode(&gzip(&changed, Compression::default()).unwrap(), None).is_err());
        }
        for name in [
            "../escape",
            "/absolute",
            "project/../escape",
            "project//escape",
            "project/./escape",
            "project/a\\b",
        ] {
            let mut changed = raw.clone();
            changed[..100].fill(0);
            changed[..name.len()].copy_from_slice(name.as_bytes());
            let mut header = tar::Header::from_byte_slice(&changed[..512]).clone();
            header.set_cksum();
            changed[..512].copy_from_slice(header.as_bytes());
            assert!(
                decode(&gzip(&changed, Compression::default()).unwrap(), None).is_err(),
                "{name}"
            );
        }
        let mut duplicate = raw[..raw.len() - 1024].to_vec();
        duplicate.extend_from_slice(&raw);
        assert!(decode(&gzip(&duplicate, Compression::default()).unwrap(), None).is_err());
        let mut trailer = raw.clone();
        trailer.extend_from_slice(&raw);
        assert!(decode(&gzip(&trailer, Compression::default()).unwrap(), None).is_err());
        let truncated = &raw[..raw.len() - 512];
        assert!(decode(&gzip(truncated, Compression::default()).unwrap(), None).is_err());
    }
    #[test]
    fn gzip_corruption_concatenation_and_bombs_are_bounded() {
        let valid = encode(&entries());
        let mut corrupt = valid.clone();
        let end = corrupt.len() - 8;
        corrupt[end] ^= 1;
        assert!(decode(&corrupt, None).is_err());
        let mut concatenated = valid.clone();
        concatenated.extend_from_slice(&valid);
        assert!(decode(&concatenated, None).is_err());
        let mut trailing = valid.clone();
        trailing.push(0);
        assert!(decode(&trailing, None).is_err());
        for n in [0, 1, 10, valid.len() - 1] {
            assert!(decode(&valid[..n], None).is_err());
        }
        let bomb = gzip(&vec![0; 16 * 1024 * 1024], Compression::best()).unwrap();
        assert!(decode(&bomb, None).is_err());
        let mut many = entries();
        for n in 0..=MAX_FILES {
            many.insert(format!("project/f{n}"), vec![]);
        }
        assert!(decode(&encode(&many), None).is_err());
        let mut huge = entries();
        huge.insert("project/huge".into(), vec![0; MAX_FILE as usize + 1]);
        assert!(
            decode(
                &gzip(&tar_bytes(&huge).unwrap(), Compression::none()).unwrap(),
                None
            )
            .is_err()
        );
    }
    #[test]
    fn gzip_envelope_cannot_hide_host_metadata() {
        let raw = tar_bytes(&entries()).unwrap();
        for builder in [
            GzBuilder::new().filename("private-host-path"),
            GzBuilder::new().comment("hidden"),
            GzBuilder::new().mtime(1),
            GzBuilder::new().extra(vec![1, 2, 3]),
        ] {
            let mut encoder = builder
                .operating_system(255)
                .write(Vec::new(), Compression::default());
            encoder.write_all(&raw).unwrap();
            assert!(decode(&encoder.finish().unwrap(), None).is_err());
        }
    }
    #[test]
    fn valid_highly_compressible_payload_uses_bounded_transport() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("supabricks.toml"),"format_version=2\nid='707f5d76-71f2-4997-9bc0-71e375856031'\nname='compressible'\n[package]\nversion='0.1.0'\ninclude=['fixture.txt']\nnotebook_outputs='strip'\n").unwrap();
        std::fs::write(root.path().join("fixture.txt"), vec![b'0'; 4 * 1024 * 1024]).unwrap();
        let out = tempfile::tempdir().unwrap();
        let path = out.path().join("large.sbproj");
        pack(root.path(), &path, None).unwrap();
        assert!(verify(&path, None).is_ok());
    }
}

/// Fork portable source into a new private, unbound definition; never mutate input.
pub fn fork(
    directory: &Path,
    destination: &Path,
    name: &str,
    target: Option<&str>,
) -> Result<Value> {
    let id = supabricks_core::resource::ProjectId::new();
    super::manifest::identity(id, name)?;
    let temporary = tempfile::tempdir()?;
    let archive = temporary.path().join("source.sbproj");
    pack(directory, &archive, target)?;
    let (report, mut files) = read(&archive, target)?;
    let mut manifest: toml::Value =
        super::manifest::parse(&files["supabricks.toml"], "supabricks.toml")?;
    manifest["id"] = toml::Value::String(id.to_string());
    manifest["name"] = toml::Value::String(name.into());
    files.insert(
        "supabricks.toml".into(),
        toml::to_string_pretty(&manifest)?.into_bytes(),
    );
    let mut memory = Source::memory(files.clone())?;
    let inspection = inspect_inputs(&mut memory, Some(&report.inspection.target))?;
    let publication = super::publication::Publication::new(destination)?;
    for (path, bytes) in files {
        publication.write(&path, &bytes)?;
    }
    publication.write(MARKER,&canonical(&json!({"format_version":1,"state":"unbound","origin_definition_id":report.inspection.definition.id,"origin_content_sha256":report.content_sha256,"definition_id":id}))?)?;
    publication.publish_directory()?;
    Ok(
        json!({"api_version":1,"definition_id":id,"origin_definition_id":report.inspection.definition.id,"unbound":true,"inspection":inspection}),
    )
}
