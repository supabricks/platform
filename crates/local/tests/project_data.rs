use serde_json::json;
use supabricks_core::resource::{BranchId, ProjectId};
use supabricks_local::projects::data::*;

fn fixture() -> Content {
    Content {
        format_version: 1,
        profile: "postgres_tables".into(),
        postgres_major: 17,
        locale: Locale {
            provider: "b".into(),
            collate: "C.UTF-8".into(),
            ctype: "C.UTF-8".into(),
            locale: Some("C.UTF-8".into()),
            version: Some("1".into()),
        },
        source: Provenance {
            definition_id: ProjectId::new(),
            source_sha256: "a".repeat(64),
            runtime_project_id: ProjectId::new(),
            branch_id: BranchId::new(),
            branch_revision: 1,
            snapshot: "2:3:".into(),
        },
        tables: vec![Table {
            table: TableName {
                schema: "public".into(),
                name: "orders".into(),
            },
            columns: vec![
                Column {
                    name: "id".into(),
                    data_type: Type::Integer,
                    nullable: false,
                },
                Column {
                    name: "text".into(),
                    data_type: Type::Text,
                    nullable: true,
                },
            ],
            constraints: vec![Constraint {
                name: "orders_pkey".into(),
                kind: ConstraintKind::PrimaryKey,
                columns: vec!["id".into()],
            }],
            rows: 2,
            data_sha256: hash(b"1\t\\N\n2\t\\\\N\\tline\\nnext\n"),
            copy_text: "1\t\\N\n2\t\\\\N\\tline\\nnext\n".into(),
        }],
    }
}

#[test]
fn locale_matching_is_required_exactly_for_collatable_column_types() {
    let mut content = fixture();
    assert!(content.requires_matching_locale());
    content.tables[0].columns[1].data_type = Type::Varchar { length: Some(32) };
    assert!(content.requires_matching_locale());
    for ty in [
        Type::Bytea,
        Type::Json,
        Type::Jsonb,
        Type::Integer,
        Type::Numeric {
            precision: None,
            scale: None,
        },
        Type::TimestampTz,
    ] {
        content.tables[0].columns[1].data_type = ty;
        assert!(!content.requires_matching_locale());
    }
}

#[test]
fn deterministic_typed_archive_preserves_copy_bytes_without_reporting_values() {
    let source = fixture();
    let bytes = encode(source.clone()).unwrap();
    assert_eq!(bytes, encode(source).unwrap());
    let read = decode(&bytes).unwrap();
    assert_eq!(
        read.content.tables[0].copy_text,
        "1\t\\N\n2\t\\\\N\\tline\\nnext\n"
    );
    assert!(read.report()["tables"][0].get("copy_text").is_none());
    assert_eq!(read.archive_sha256, hash(&bytes));
}

#[test]
fn changed_or_unknown_archive_content_is_rejected() {
    let bytes = encode(fixture()).unwrap();
    let original: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    for change in ["digest", "data", "sql", "profile", "type"] {
        let mut v = original.clone();
        match change {
            "digest" => v["content_sha256"] = json!("0".repeat(64)),
            "data" => v["content"]["tables"][0]["copy_text"] = json!("3\tchanged\n"),
            "sql" => v["content"]["sql"] = json!("DROP TABLE other"),
            "profile" => v["content"]["profile"] = json!("delta"),
            _ => {
                v["content"]["tables"][0]["columns"][0]["data_type"] =
                    json!({"kind":"sql","value":"custom_type"})
            }
        }
        assert!(
            decode(&serde_json::to_vec(&v).unwrap()).is_err(),
            "{change}"
        );
    }
}

#[test]
fn duplicate_names_bad_constraints_control_tables_and_copy_terminators_fail() {
    for case in 0..7 {
        let mut v = fixture();
        match case {
            0 => v.tables.push(v.tables[0].clone()),
            1 => v.tables[0].columns[1].name = "id".into(),
            2 => v.tables[0].constraints[0].columns = vec!["missing".into()],
            3 => v.tables[0].table.schema = "_supabricks".into(),
            4 => v.tables[0].rows = 3,
            5 => {
                v.tables[0].copy_text = "\\.\n".into();
                v.tables[0].data_sha256 = hash(b"\\.\n");
            }
            _ => v.tables[0].columns[0].nullable = true,
        }
        assert!(encode(v).is_err(), "case {case}");
    }
}

#[test]
fn byte_row_and_schema_limits_fail_before_database_access() {
    let mut source = fixture();
    source.tables[0].copy_text = format!("1\t{}\n", "x".repeat(MAX_ROW));
    source.tables[0].rows = 1;
    source.tables[0].data_sha256 = hash(source.tables[0].copy_text.as_bytes());
    assert!(encode(source).is_err());
    let mut source = fixture();
    source.tables[0].rows = u64::MAX;
    assert!(encode(source).is_err());
    let mut source = fixture();
    source.tables[0].columns[0].data_type = Type::Numeric {
        precision: Some(1001),
        scale: Some(0),
    };
    assert!(encode(source).is_err());
}

#[test]
fn offline_cli_verifies_without_home_or_daemon_and_refuses_symlink() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("sales.sbdata");
    std::fs::write(&path, encode(fixture()).unwrap()).unwrap();
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_supabricks"))
        .env_clear()
        .current_dir(temp.path())
        .args(["project", "data", "verify"])
        .arg(&path)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        serde_json::from_slice::<serde_json::Value>(&output.stdout).unwrap()["verified"]
            .as_bool()
            .unwrap()
    );
    let link = temp.path().join("link");
    std::os::unix::fs::symlink(&path, &link).unwrap();
    assert!(read(&link).is_err());
    assert!(read(temp.path()).is_err());
}
