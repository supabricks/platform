//! Revision-fenced public manifest editing; applying destination bindings is separate.
use super::*;
use crate::notebooks::files::directory::Directory;
use std::{
    ffi::OsStr,
    io::{Read, Write},
    os::unix::fs::MetadataExt,
};
pub(super) fn edit(
    store: &Store,
    binding: &Binding,
    logical: &str,
    requirement: Option<&str>,
    expected: &str,
) -> Result<Value> {
    let context = store.binding_context(binding)?;
    if context.legacy {
        return Err(conflict("dataset declarations require a format-2 project"));
    }
    let name = logical
        .strip_prefix("dataset.")
        .filter(|n| {
            !n.is_empty()
                && n.len() <= 63
                && n.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b))
        })
        .ok_or_else(|| invalid("expected dataset.NAME"))?;
    if requirement.is_some_and(|r| r.is_empty() || r.len() > 256 || r.chars().any(char::is_control))
    {
        return Err(invalid("invalid dataset requirement"));
    }
    let dir = Directory::project(&binding.worktree)?;
    let filename = OsStr::new("supabricks.toml");
    let read = || -> Result<Vec<u8>> {
        let file = dir.open(filename, libc::O_RDONLY)?;
        let m = file.metadata()?;
        if !m.is_file() || m.nlink() != 1 || m.len() > 1024 * 1024 {
            return Err(invalid(
                "manifest must be a bounded regular file without hard links",
            ));
        }
        let mut bytes = vec![];
        file.take(1024 * 1024 + 1).read_to_end(&mut bytes)?;
        Ok(bytes)
    };
    let before = read()?;
    if hex::encode(Sha256::digest(&before)) != expected {
        return Err(conflict(
            "manifest changed; reload before editing dataset requirements",
        ));
    }
    let text = std::str::from_utf8(&before).map_err(|_| invalid("manifest is not UTF-8"))?;
    let mut doc = text
        .parse::<toml_edit::DocumentMut>()
        .map_err(|_| invalid("manifest is not editable TOML"))?;
    let mut original = crate::projects::source::Source::new(&binding.worktree)?;
    let report = crate::projects::inspect_inputs(&mut original, Some(&context.target))?;
    let root_has = doc
        .get("resources")
        .and_then(|r| r.get("dataset"))
        .and_then(|r| r.get(name))
        .is_some();
    if report.resources.contains_key(logical) && !root_has {
        return Err(conflict(
            "dataset is declared in an included fragment; edit that source file explicitly",
        ));
    }
    if let Some(requirement) = requirement {
        if !root_has {
            doc["resources"]["dataset"][name]["kind"] = toml_edit::value("catalog_dataset");
        }
        doc["resources"]["dataset"][name]["requirement"] = toml_edit::value(requirement);
    } else if root_has {
        doc["resources"]["dataset"]
            .as_table_like_mut()
            .ok_or_else(|| invalid("invalid dataset declarations"))?
            .remove(name);
    }
    let bytes = doc.to_string().into_bytes();
    // Validate the complete source graph before publishing the single-file edit.
    original.verify()?;
    let mut files = original.payload;
    files.insert("supabricks.toml".into(), bytes.clone());
    let mut source = crate::projects::source::Source::memory(files)?;
    crate::projects::inspect_inputs(&mut source, Some(&context.target))?;
    let temp = format!(".supabricks-dataset-{}.tmp", OperationId::new());
    let temp = OsStr::new(&temp);
    let mut file = dir.open(temp, libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL)?;
    let result = (|| -> Result<()> {
        file.write_all(&bytes)?;
        file.sync_all()?;
        if read()? != before {
            return Err(conflict("manifest changed while editing; reload"));
        }
        dir.publish(temp, filename, true)
    })();
    let _ = dir.unlink(temp);
    result?;
    Ok(
        json!({"api_version":1,"logical":logical,"manifest_sha256":hex::encode(Sha256::digest(&bytes)),"applied":false}),
    )
}
