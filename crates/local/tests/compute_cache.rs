use std::fs;
use supabricks_core::spec::ComputeCacheProfile::{Compact, SourceLoad};
use supabricks_local::{engine::RuntimeConfig, store::Store};

#[test]
fn profile_is_persistent_bounded_and_legacy_defaults_to_compact() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("cell");
    let store = Store::open(&root).unwrap();
    let bundle = temp.path().join("engine");
    let helpers = temp.path().join("helpers");
    fs::create_dir(&bundle).unwrap();
    fs::create_dir(&helpers).unwrap();
    for name in [
        "bin/pageserver",
        "bin/safekeeper",
        "bin/storage_broker",
        "bin/compute_ctl",
        "pg_install/v17/bin/postgres",
    ] {
        let path = bundle.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, b"").unwrap();
    }
    for name in ["process-compose", "weed"] {
        fs::write(helpers.join(name), b"").unwrap();
    }
    RuntimeConfig::initialize_with_cache(&store, &bundle, &helpers, SourceLoad).unwrap();
    let path = root.join("runtime.json");
    let before = fs::read(&path).unwrap();
    RuntimeConfig::initialize(&store, &bundle, &helpers).unwrap();
    RuntimeConfig::initialize_with_cache(&store, &bundle, &helpers, SourceLoad).unwrap();
    assert!(RuntimeConfig::initialize_with_cache(&store, &bundle, &helpers, Compact).is_err());
    assert_eq!(fs::read(&path).unwrap(), before);
    let mut value: serde_json::Value = serde_json::from_slice(&before).unwrap();
    assert_eq!(value["compute_cache_profile"], "source-load");
    value
        .as_object_mut()
        .unwrap()
        .remove("compute_cache_profile");
    let legacy: RuntimeConfig = serde_json::from_value(value.clone()).unwrap();
    assert_eq!(legacy.compute_cache_profile, Compact);
    for invalid in [
        serde_json::json!("unlimited"),
        serde_json::json!(8192),
        serde_json::Value::Null,
    ] {
        value["compute_cache_profile"] = invalid;
        assert!(serde_json::from_value::<RuntimeConfig>(value.clone()).is_err());
    }
}
