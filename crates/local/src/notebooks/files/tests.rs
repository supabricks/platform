use super::*;
use std::fs;
fn document() -> Value {
    json!({"cells":[{"id":"a","cell_type":"code","metadata":{},"source":"print('hello')","outputs":[],"execution_count":null}],"metadata":{},"nbformat":4,"nbformat_minor":5})
}
fn save(project: &Path, path: &str, document: Value, revision: Option<String>) -> Result<Value> {
    handle(
        project,
        Command::Save {
            path: path.into(),
            document,
            expected_revision: revision,
        },
    )
}
#[test]
fn repeated_saves_and_external_conflicts_use_exact_string_revisions() {
    let dir = tempfile::tempdir().unwrap();
    let first = save(dir.path(), "a.ipynb", document(), None).unwrap();
    assert!(first["revision"].is_string());
    let revision = first["revision"].as_str().unwrap().to_owned();
    let encoded=serde_json::to_string(&json!({"action":"save","path":"a.ipynb","document":document(),"expected_revision":revision})).unwrap();
    assert!(handle(dir.path(), serde_json::from_str(&encoded).unwrap()).is_ok());
    assert!(save(dir.path(), "a.ipynb", document(), None).is_err());
    fs::write(
        dir.path().join("notebooks/a.ipynb"),
        serde_json::to_vec(&json!({"external":"edit"})).unwrap(),
    )
    .unwrap();
    assert!(save(dir.path(), "a.ipynb", document(), Some(revision)).is_err());
    assert_eq!(
        fs::read_to_string(dir.path().join("notebooks/a.ipynb")).unwrap(),
        "{\"external\":\"edit\"}"
    );
}
#[test]
fn symlink_root_ancestors_and_leaf_are_refused() {
    use std::os::unix::fs::symlink;
    let project = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    symlink(outside.path(), project.path().join("notebooks")).unwrap();
    assert!(save(project.path(), "escape.ipynb", document(), None).is_err());
    assert!(handle(project.path(), Command::List).is_err());
    fs::remove_file(project.path().join("notebooks")).unwrap();
    fs::create_dir(project.path().join("notebooks")).unwrap();
    symlink(outside.path(), project.path().join("notebooks/link")).unwrap();
    assert!(save(project.path(), "link/escape.ipynb", document(), None).is_err());
    fs::write(
        outside.path().join("escape.ipynb"),
        serde_json::to_vec(&document()).unwrap(),
    )
    .unwrap();
    assert!(
        handle(
            project.path(),
            Command::Get {
                path: "link/escape.ipynb".into()
            }
        )
        .is_err()
    );
    symlink(
        outside.path().join("escape.ipynb"),
        project.path().join("notebooks/leaf.ipynb"),
    )
    .unwrap();
    assert!(
        handle(
            project.path(),
            Command::Get {
                path: "leaf.ipynb".into()
            }
        )
        .is_err()
    );
    assert!(save(project.path(), "leaf.ipynb", document(), None).is_err());
}
#[test]
fn directory_replacement_cannot_redirect_an_open_parent() {
    let project = tempfile::tempdir().unwrap();
    let outside = tempfile::tempdir().unwrap();
    let p = relative_path("nested/a.ipynb").unwrap();
    let dir = parent(project.path(), &p, true).unwrap();
    fs::rename(
        project.path().join("notebooks/nested"),
        project.path().join("notebooks/original"),
    )
    .unwrap();
    std::os::unix::fs::symlink(outside.path(), project.path().join("notebooks/nested")).unwrap();
    dir.open(
        OsStr::new("a.ipynb"),
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
    )
    .unwrap();
    assert!(!outside.path().join("a.ipynb").exists());
}
#[test]
fn list_filters_companion_files_and_save_cleans_temporary_files() {
    let p = tempfile::tempdir().unwrap();
    save(p.path(), "nested/a.ipynb", document(), None).unwrap();
    fs::write(p.path().join("notebooks/README.md"), "notes").unwrap();
    fs::write(p.path().join("notebooks/.abandoned.tmp"), "partial").unwrap();
    assert_eq!(
        handle(p.path(), Command::List).unwrap()["files"],
        json!(["nested/a.ipynb"])
    );
    assert_eq!(
        fs::read_dir(p.path().join("notebooks/nested"))
            .unwrap()
            .count(),
        1
    );
    let dir = parent(p.path(), Path::new("nested/a.ipynb"), false).unwrap();
    dir.open(
        OsStr::new("temp"),
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL,
    )
    .unwrap();
    assert!(
        dir.publish(OsStr::new("temp"), OsStr::new("a.ipynb"), false)
            .is_err()
    );
    assert_eq!(
        handle(
            p.path(),
            Command::Get {
                path: "nested/a.ipynb".into()
            }
        )
        .unwrap()["document"],
        document()
    );
}
#[test]
fn malformed_sources_schema_and_oversized_files_are_rejected() {
    let mut d = document();
    d["cells"][0]["source"] = json!([null, "x"]);
    assert!(validate_document(&d).is_err());
    d = document();
    d["cells"][0]["source"] = json!(["line 1\n", "line 2"]);
    assert!(validate_document(&d).is_ok());
    d = document();
    d["cells"][0].as_object_mut().unwrap().remove("outputs");
    assert!(validate_document(&d).is_err());
    d = document();
    d["cells"][0]["outputs"] = json!([{"output_type":"stream","name":"stdout","text":"hello"}]);
    assert!(validate_document(&d).is_ok());
    let duplicate = d["cells"][0].clone();
    d["cells"].as_array_mut().unwrap().push(duplicate);
    assert!(validate_document(&d).is_err());
    let p = tempfile::tempdir().unwrap();
    fs::create_dir(p.path().join("notebooks")).unwrap();
    fs::File::create(p.path().join("notebooks/large.ipynb"))
        .unwrap()
        .set_len(1 << 30)
        .unwrap();
    assert!(
        handle(
            p.path(),
            Command::Get {
                path: "large.ipynb".into()
            }
        )
        .is_err()
    );
    for path in [
        "../a.ipynb",
        "/a.ipynb",
        "a/../b.ipynb",
        "a.json",
        "./a.ipynb",
    ] {
        assert!(relative_path(path).is_err());
    }
}
