//! Bounded notebook documents and conditional, atomic project saves.
pub(crate) mod directory;
use crate::store::{
    Result,
    error::{conflict, invalid},
};
use directory::Directory;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    ffi::OsStr,
    io::{Read, Write},
    path::{Component, Path, PathBuf},
};

pub const MAX_DOCUMENT_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_CELLS: usize = 512;
pub const MAX_CELL_SOURCE_BYTES: usize = 512 * 1024;

pub fn root(worktree: &Path) -> PathBuf {
    worktree.join("notebooks")
}
pub fn relative_path(path: &str) -> Result<PathBuf> {
    if path.is_empty()
        || path.len() > 256
        || path.contains('\0')
        || path
            .split('/')
            .any(|v| v.is_empty() || v == "." || v == "..")
    {
        return Err(invalid("Notebook path must be relative without traversal"));
    }
    let p = Path::new(path);
    if p.extension().and_then(|v| v.to_str()) != Some("ipynb")
        || p.components().any(|v| !matches!(v, Component::Normal(_)))
    {
        return Err(invalid("Notebook path must be a relative .ipynb file"));
    }
    Ok(p.into())
}
fn source(v: &Value, limit: usize) -> Result<()> {
    let size = match v {
        Value::String(s) => s.len(),
        Value::Array(lines) => lines.iter().try_fold(0usize, |total, line| {
            let s = line
                .as_str()
                .ok_or_else(|| invalid("Notebook source must contain only strings"))?;
            total
                .checked_add(s.len())
                .filter(|n| *n <= limit)
                .ok_or_else(|| invalid("Notebook source exceeds limit"))
        })?,
        _ => {
            return Err(invalid(
                "Notebook source must be a string or array of strings",
            ));
        }
    };
    if size > limit {
        return Err(invalid("Notebook source exceeds limit"));
    }
    Ok(())
}
fn output(v: &Value) -> Result<()> {
    let o = v
        .as_object()
        .ok_or_else(|| invalid("Invalid notebook output"))?;
    match o.get("output_type").and_then(Value::as_str) {
        Some("stream") if matches!(v["name"].as_str(), Some("stdout" | "stderr")) => {
            source(&v["text"], MAX_DOCUMENT_BYTES)?
        }
        Some("display_data" | "execute_result")
            if v["data"].is_object() && v["metadata"].is_object() =>
        {
            if v["output_type"] == "execute_result" && v["execution_count"].as_u64().is_none() {
                return Err(invalid("Invalid execution count"));
            }
        }
        Some("error")
            if v["ename"].is_string()
                && v["evalue"].is_string()
                && v["traceback"]
                    .as_array()
                    .is_some_and(|a| a.iter().all(Value::is_string)) =>
        {
            ()
        }
        _ => return Err(invalid("Invalid notebook output")),
    }
    Ok(())
}
fn provenance(value: &Value) -> Result<()> {
    if let Some(environment) = value.get("environment").filter(|v| !v.is_null()) {
        let identity: crate::environments::Identity =
            serde_json::from_value(environment.clone())
                .map_err(|_| invalid("Invalid notebook environment provenance"))?;
        for hash in [
            &identity.inputs.manifest,
            &identity.inputs.lock,
            &identity.contract,
            &identity.inventory,
        ] {
            if hash.len() != 64 || !hash.bytes().all(|c| c.is_ascii_hexdigit()) {
                return Err(invalid("Invalid notebook environment fingerprint"));
            }
        }
    }
    Ok(())
}
pub fn validate_document(v: &Value) -> Result<()> {
    if v["nbformat"].as_u64() != Some(4)
        || !v["nbformat_minor"].as_u64().is_some_and(|n| n <= 5)
        || !v["metadata"].is_object()
    {
        return Err(invalid(
            "Unsupported notebook format (expected nbformat 4.0–4.5)",
        ));
    }
    provenance(&v["metadata"]["supabricks"]["binding"])?;
    provenance(&v["metadata"]["supabricks"]["outputs"])?;
    let cells = v["cells"]
        .as_array()
        .ok_or_else(|| invalid("Notebook cells must be an array"))?;
    if cells.len() > MAX_CELLS {
        return Err(invalid("Notebook has too many cells"));
    }
    let mut ids = HashSet::new();
    for c in cells {
        if !c.is_object() || !c["metadata"].is_object() {
            return Err(invalid("Notebook cell requires metadata"));
        }
        provenance(&c["metadata"]["supabricks_outputs"])?;
        source(&c["source"], MAX_CELL_SOURCE_BYTES)?;
        if v["nbformat_minor"] == 5 || c.get("id").is_some() {
            let id = c["id"]
                .as_str()
                .ok_or_else(|| invalid("Notebook cell requires an ID"))?;
            if id.is_empty()
                || id.len() > 64
                || !id
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
                || !ids.insert(id)
            {
                return Err(invalid("Notebook cell IDs must be valid and unique"));
            }
        }
        match c["cell_type"].as_str() {
            Some("code") => {
                if c.get("execution_count").is_none()
                    || !(c["execution_count"].is_null() || c["execution_count"].as_u64().is_some())
                {
                    return Err(invalid("Code cell requires an execution count or null"));
                }
                for o in c["outputs"]
                    .as_array()
                    .ok_or_else(|| invalid("Code cell requires outputs"))?
                {
                    output(o)?;
                }
            }
            Some("markdown" | "raw") => (),
            _ => return Err(invalid("Unsupported notebook cell type")),
        }
    }
    if serde_json::to_vec(v)?.len() > MAX_DOCUMENT_BYTES {
        return Err(invalid("Notebook document is too large"));
    }
    Ok(())
}
fn parent(worktree: &Path, path: &Path, create: bool) -> Result<Directory> {
    let mut dir = Directory::project(worktree)?.child(OsStr::new("notebooks"), create)?;
    for part in path.parent().unwrap().components() {
        dir = dir.child(part.as_os_str(), create)?;
    }
    Ok(dir)
}
fn read(dir: &Directory, name: &OsStr) -> Result<Vec<u8>> {
    let file = dir.open(name, libc::O_RDONLY)?;
    if !file.metadata()?.is_file() {
        return Err(conflict("Notebook is not a regular file"));
    }
    let mut bytes = Vec::new();
    file.take(MAX_DOCUMENT_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_DOCUMENT_BYTES {
        return Err(invalid("Notebook document is too large"));
    }
    Ok(bytes)
}
fn revision(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}
fn expected(dir: &Directory, name: &OsStr, want: Option<&str>) -> Result<()> {
    match want {
        Some(want) => {
            if revision(&read(dir, name)?) != want {
                return Err(conflict(
                    "Notebook changed on disk; download your edits or reload before saving",
                ));
            }
        }
        None if dir.exists(name)? => {
            return Err(conflict("Notebook already exists; open it before saving"));
        }
        None => (),
    }
    Ok(())
}
#[derive(Debug, serde::Serialize, serde::Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Rename {
        path: String,
        destination: String,
        expected_revision: String,
    },
    List,
    Get {
        path: String,
    },
    Save {
        path: String,
        document: Value,
        expected_revision: Option<String>,
    },
}
pub fn handle(worktree: &Path, command: Command) -> Result<Value> {
    match command {
        Command::Rename {
            path,
            destination,
            expected_revision,
        } => {
            let from = relative_path(&path)?;
            let to = relative_path(&destination)?;
            let source = parent(worktree, &from, false)?;
            let dest = parent(worktree, &to, true)?;
            expected(&source, from.file_name().unwrap(), Some(&expected_revision))?;
            if from != to {
                source.move_to(from.file_name().unwrap(), &dest, to.file_name().unwrap())?;
            }
            Ok(json!({"path":destination,"revision":expected_revision}))
        }
        Command::List => {
            let project = Directory::project(worktree)?;
            if !project.exists(OsStr::new("notebooks"))? {
                return Ok(json!({"files":[]}));
            }
            let mut pending = vec![(
                project.child(OsStr::new("notebooks"), false)?,
                String::new(),
            )];
            let mut budget = 4096;
            let mut files = Vec::new();
            while let Some((dir, prefix)) = pending.pop() {
                for (name, kind) in dir.entries(&mut budget)? {
                    let path = format!("{prefix}{name}");
                    if kind == libc::S_IFDIR as u32 {
                        if path.len() > 256 {
                            return Err(invalid("Notebook directory path is too long"));
                        }
                        pending.push((dir.child(OsStr::new(&name), false)?, format!("{path}/")));
                    } else if kind == libc::S_IFREG as u32 && name.ends_with(".ipynb") {
                        relative_path(&path)?;
                        files.push(path);
                    }
                }
            }
            files.sort();
            Ok(json!({"files":files}))
        }
        Command::Get { path } => {
            let p = relative_path(&path)?;
            let dir = parent(worktree, &p, false)?;
            let bytes = read(&dir, p.file_name().unwrap())?;
            let document = serde_json::from_slice(&bytes)?;
            validate_document(&document)?;
            Ok(json!({"path":path,"document":document,"revision":revision(&bytes)}))
        }
        Command::Save {
            path,
            document,
            expected_revision,
        } => {
            validate_document(&document)?;
            let p = relative_path(&path)?;
            let dir = parent(worktree, &p, true)?;
            let name = p.file_name().unwrap();
            expected(&dir, name, expected_revision.as_deref())?;
            let bytes = serde_json::to_vec_pretty(&document)?;
            if bytes.len() > MAX_DOCUMENT_BYTES {
                return Err(invalid("Formatted notebook document is too large"));
            }
            let temporary = format!(
                ".supabricks-{}.tmp",
                supabricks_core::resource::OperationId::new()
            );
            let temporary = OsStr::new(&temporary);
            let mut file = dir.open(temporary, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
            let result = (|| -> Result<()> {
                file.write_all(&bytes)?;
                file.sync_all()?;
                expected(&dir, name, expected_revision.as_deref())?;
                dir.publish(temporary, name, expected_revision.is_some())
            })();
            let _ = dir.unlink(temporary);
            result?;
            Ok(json!({"path":path,"saved":true,"revision":revision(&bytes)}))
        }
    }
}
#[cfg(test)]
mod tests;
