//! Validation and atomic persistence for project notebook documents.
//!
//! The HTTP adapter will call this module after authenticating the console
//! session. Keeping path and document policy here prevents the browser layer
//! from becoming a second filesystem implementation.
use crate::store::{
    Result,
    error::{conflict, invalid},
};
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::{Component, Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub const MAX_DOCUMENT_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_CELLS: usize = 512;
pub const MAX_CELL_SOURCE_BYTES: usize = 512 * 1024;

pub fn root(worktree: &Path) -> PathBuf {
    worktree.join("notebooks")
}

pub fn relative_path(path: &str) -> Result<PathBuf> {
    if path.is_empty() || path.len() > 256 || path.contains('\0') {
        return Err(invalid("invalid notebook path"));
    }
    let candidate = Path::new(path);
    if candidate.extension().and_then(|v| v.to_str()) != Some("ipynb") {
        return Err(invalid("notebook files must use the .ipynb extension"));
    }
    let mut clean = PathBuf::new();
    for component in candidate.components() {
        match component {
            Component::Normal(part) => clean.push(part),
            _ => {
                return Err(invalid(
                    "notebook path must be relative and contain no traversal",
                ));
            }
        }
    }
    if clean.as_os_str().is_empty() {
        return Err(invalid("invalid notebook path"));
    }
    Ok(clean)
}

pub fn validate_document(value: &Value) -> Result<()> {
    let object = value
        .as_object()
        .ok_or_else(|| invalid("notebook document must be an object"))?;
    if object.get("nbformat").and_then(Value::as_u64) != Some(4)
        || object
            .get("nbformat_minor")
            .and_then(Value::as_u64)
            .is_none()
        || object.get("metadata").and_then(Value::as_object).is_none()
    {
        return Err(invalid("unsupported notebook format"));
    }
    let cells = object
        .get("cells")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("notebook cells must be an array"))?;
    if cells.len() > MAX_CELLS {
        return Err(invalid("notebook has too many cells"));
    }
    for cell in cells {
        let cell = cell
            .as_object()
            .ok_or_else(|| invalid("notebook cell must be an object"))?;
        if !matches!(
            cell.get("cell_type").and_then(Value::as_str),
            Some("code" | "markdown" | "raw")
        ) {
            return Err(invalid("unsupported notebook cell type"));
        }
        let source = cell
            .get("source")
            .ok_or_else(|| invalid("notebook cell has no source"))?;
        let bytes = match source {
            Value::String(s) => s.len(),
            Value::Array(lines) => lines
                .iter()
                .map(|v| v.as_str().map(str::len).unwrap_or(usize::MAX))
                .sum(),
            _ => usize::MAX,
        };
        if bytes > MAX_CELL_SOURCE_BYTES {
            return Err(invalid("notebook cell source is too large"));
        }
    }
    let bytes = serde_json::to_vec(value)?;
    if bytes.len() > MAX_DOCUMENT_BYTES {
        return Err(invalid("notebook document is too large"));
    }
    Ok(())
}

pub fn load(path: &Path) -> Result<Value> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.file_type().is_file() {
        return Err(conflict("notebook path is not a regular file"));
    }
    let bytes = fs::read(path)?;
    if bytes.len() > MAX_DOCUMENT_BYTES {
        return Err(invalid("notebook document is too large"));
    }
    let value = serde_json::from_slice(&bytes)?;
    validate_document(&value)?;
    Ok(value)
}

pub fn save(path: &Path, value: &Value) -> Result<()> {
    validate_document(value)?;
    let parent = path
        .parent()
        .ok_or_else(|| invalid("invalid notebook path"))?;
    fs::create_dir_all(parent)?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = parent.join(format!(".supabricks-{nonce}.tmp"));
    let bytes = serde_json::to_vec_pretty(value)?;
    {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
    }
    fs::rename(&temporary, path)?;
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn document() -> Value {
        json!({"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5})
    }
    #[test]
    fn rejects_traversal_and_non_notebooks() {
        assert!(relative_path("../x.ipynb").is_err());
        assert!(relative_path("x.json").is_err());
        assert!(relative_path("a/x.ipynb").is_ok());
    }
    #[test]
    fn validates_and_round_trips_atomically() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notebooks/a.ipynb");
        save(&path, &document()).unwrap();
        assert_eq!(load(&path).unwrap(), document());
    }
    #[test]
    fn rejects_invalid_schema() {
        let mut value = document();
        value["nbformat"] = json!(3);
        assert!(validate_document(&value).is_err());
    }
}
