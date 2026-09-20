use super::*;
use crate::{deployments::Context, store::Snapshot};
use std::collections::BTreeSet;

pub fn query() -> crate::query::Query {
    crate::query::Query {sql:r#"SELECT json_build_object('oid',c.oid::text,'incarnation',json_build_array(c.oid::text,c.relfilenode::text,(SELECT oid::text FROM pg_database WHERE datname=current_database())), 'schema',n.nspname,'name',c.relname,'column_count',(SELECT count(*) FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped),'columns',(SELECT json_agg(json_build_object('name',a.attname,'data_type',format_type(a.atttypid,a.atttypmod),'nullable',NOT a.attnotnull,'ordinal',a.attnum) ORDER BY a.attnum) FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped))::text FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema','_supabricks') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%' AND has_table_privilege(c.oid,'SELECT') ORDER BY n.nspname,c.relname LIMIT 129"#.into(),read_only:true,max_rows:129,timeout_ms:10000}
}
fn base(context: &Context, branch: BranchId, provider: &str, provider_id: &str) -> Asset {
    Asset {
        id: OperationId::new(),
        project_id: context.runtime_project_id,
        deployment_id: context.deployment_id,
        branch_id: branch,
        resource_key: String::new(),
        provider_id: provider_id.into(),
        provider: provider.into(),
        kind: String::new(),
        incarnation: String::new(),
        uc_object_id: None,
        alias: String::new(),
        schema: String::new(),
        name: String::new(),
        columns: vec![],
        version: String::new(),
        publication_revision: None,
        epoch_id: None,
        source_revision: None,
        observed_at_ms: chrono::Utc::now().timestamp_millis(),
        snapshot_at_ms: None,
        state: "active".into(),
    }
}
fn label(value: &Value) -> RemoteResult<String> {
    value
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control))
        .map(str::to_owned)
        .ok_or_else(|| Fault::new(Code::InvalidResponse, "invalid source identifier"))
}
fn finish(a: &mut Asset) -> RemoteResult<()> {
    if a.columns.is_empty() || a.columns.len() > 128 {
        return Err(Fault::new(
            Code::LimitExceeded,
            "table requires 1–128 columns",
        ));
    }
    // Always render fully quoted PostgreSQL identifiers; dots and case are labels.
    a.alias = format!(
        "\"{}\".\"{}\"",
        a.schema.replace('"', "\"\""),
        a.name.replace('"', "\"\"")
    );
    a.version = fingerprint(&json!([
        a.provider_id,
        a.resource_key,
        a.incarnation,
        a.schema,
        a.name,
        a.columns,
        a.publication_revision,
        a.source_revision
    ]));
    Ok(())
}
pub fn postgres(
    context: &Context,
    branch: BranchId,
    revision: i64,
    installation: &str,
    target: Value,
) -> RemoteResult<Vec<Asset>> {
    let result = query().run(target).map_err(|_| {
        Fault::new(
            Code::Unavailable,
            "PostgreSQL metadata query failed or exceeded its deadline",
        )
    })?;
    let rows = result["rows"].as_array().ok_or_else(|| {
        Fault::new(
            Code::InvalidResponse,
            "invalid PostgreSQL metadata response",
        )
    })?;
    if result["truncated"] == true || rows.len() > 128 {
        return Err(Fault::new(
            Code::LimitExceeded,
            "metadata exceeds the table or result byte limit",
        ));
    }
    let mut assets = vec![];
    for row in rows {
        let v: Value =
            serde_json::from_str(row[0].as_str().ok_or_else(|| {
                Fault::new(Code::InvalidResponse, "invalid PostgreSQL metadata row")
            })?)
            .map_err(|_| Fault::new(Code::InvalidResponse, "invalid PostgreSQL metadata JSON"))?;
        let mut a = base(
            context,
            branch,
            "postgres",
            &format!("postgres:{installation}:{branch}"),
        );
        a.kind = "postgres_table".into();
        a.resource_key = format!("table:{}", label(&v["oid"])?);
        a.incarnation = fingerprint(&v["incarnation"]);
        a.schema = label(&v["schema"])?;
        a.name = label(&v["name"])?;
        a.columns = serde_json::from_value(v["columns"].clone())
            .map_err(|_| Fault::new(Code::InvalidResponse, "invalid PostgreSQL column metadata"))?;
        if v["column_count"].as_u64() != Some(a.columns.len() as u64) {
            return Err(Fault::new(
                Code::InvalidResponse,
                "incomplete source schema",
            ));
        }
        a.source_revision = Some(revision);
        finish(&mut a)?;
        assets.push(a);
    }
    Ok(assets)
}
pub fn snapshot(
    context: &Context,
    branch: BranchId,
    installation: &str,
    snapshot: Snapshot,
) -> RemoteResult<Vec<Asset>> {
    if snapshot.state != "available" {
        return Err(Fault::new(
            Code::NotFound,
            "analytical snapshot is unavailable",
        ));
    }
    let p = snapshot.publication;
    let d = p
        .descriptor
        .ok_or_else(|| Fault::new(Code::InvalidResponse, "snapshot descriptor missing"))?;
    let tables = d["manifest"]["tables"]
        .as_array()
        .ok_or_else(|| Fault::new(Code::InvalidResponse, "snapshot table metadata missing"))?;
    if tables.len() > 128 {
        return Err(Fault::new(
            Code::LimitExceeded,
            "snapshot exceeds table limit",
        ));
    }
    let mut assets = vec![];
    for t in tables {
        let mut a = base(
            context,
            branch,
            "supabricks_snapshot",
            &format!("snapshot:{installation}"),
        );
        a.kind = "delta_snapshot".into();
        a.resource_key = format!("epoch:{}:table:{}", p.epoch_id, t["oid"]);
        a.incarnation = fingerprint(&json!([
            p.epoch_id,
            d["manifest_sha256"],
            t["oid"],
            t["version"]
        ]));
        a.schema = label(&t["schema"])?;
        a.name = label(&t["name"])?;
        let cols = t["columns"]
            .as_array()
            .ok_or_else(|| Fault::new(Code::InvalidResponse, "snapshot columns missing"))?;
        for (i, c) in cols.iter().enumerate() {
            a.columns.push(Column {
                name: label(&c["name"])?,
                data_type: label(&c["arrow_type"])?,
                nullable: c["nullable"].as_bool().ok_or_else(|| {
                    Fault::new(Code::InvalidResponse, "snapshot nullability missing")
                })?,
                ordinal: (i + 1) as u32,
            });
        }
        a.epoch_id = Some(p.epoch_id);
        a.publication_revision = Some(p.ordinal);
        a.source_revision = Some(p.source_revision);
        a.snapshot_at_ms = p.published_at_ms;
        finish(&mut a)?;
        assets.push(a);
    }
    Ok(assets)
}
pub fn validate_source(assets: &[Asset]) -> RemoteResult<()> {
    let mut names = BTreeSet::new();
    for a in assets.iter().filter(|a| a.kind == "postgres_table") {
        if !names.insert((a.schema.to_lowercase(), a.name.to_lowercase())) {
            return Err(Fault::new(
                Code::QuotingCollision,
                "table aliases collide under case-insensitive analytical resolution",
            ));
        }
        let mut columns = BTreeSet::new();
        for c in &a.columns {
            if !columns.insert(c.name.to_lowercase()) {
                return Err(Fault::new(
                    Code::QuotingCollision,
                    "column names collide under case-insensitive analytical resolution",
                ));
            }
        }
    }
    Ok(())
}
