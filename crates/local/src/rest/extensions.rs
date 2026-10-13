//! Extensions of a branch: which of those the installed engine ships are
//! enabled in its PostgreSQL. Like roles, this is data of the branch, read as
//! the application role and changed as the engine's control role.
use super::{
    Call, Local, Problem, Reply,
    roles::{block, change_with, read},
    shared::{cursor, fields, page, text},
    tables::{literal, place},
};
use crate::{console::workspace::identifier, store::Store};
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};
use std::collections::BTreeMap;

/// Without these the engine does not work; they are never disabled.
const REQUIRED: [&str; 2] = ["neon", "plpgsql"];
/// An extension as one JSON object, from `pg_available_extensions e`.
const EXTENSION: &str = "json_build_object('name', e.name, 'description', e.comment, \
    'default_version', e.default_version, 'installed_version', e.installed_version, \
    'schema', (SELECT n.nspname FROM pg_extension x JOIN pg_namespace n ON n.oid = x.extnamespace \
        WHERE x.extname = e.name), \
    'requires', coalesce((SELECT to_json(v.requires) FROM pg_available_extension_versions v \
        WHERE v.name = e.name AND v.version = e.default_version), '[]'::json))";
/// The engine's own tooling ships as extensions nobody is offered.
const OFFERED: &str = "e.name !~ '^neon_'";

fn no_extension() -> Problem {
    Problem::not_found("The installed engine does not ship such an extension.")
}
/// An extension named in a path, as the name it spells.
fn named(raw: &str) -> Result<String, Problem> {
    let name = percent_decode_str(raw)
        .decode_utf8()
        .map_err(|_| no_extension())?;
    identifier(&name).map_err(|_| no_extension())?;
    if name.starts_with("neon_") {
        return Err(no_extension());
    }
    Ok(name.into_owned())
}
fn refused(error: &Value) -> Problem {
    let message = error["message"].as_str().unwrap_or_default();
    let state = match message.split_once(": ") {
        Some((state, _)) if error["code"] == "sql_error" => state,
        _ => return Problem::unavailable("The database could not be reached. Try again."),
    };
    match state {
        "42704" | "58P01" | "0A000" => no_extension(),
        "3F000" => Problem::not_found("There is no such schema on this branch."),
        "2BP01" => Problem::new(
            409,
            "conflict",
            "Something in the database uses the extension. Remove that first.",
        ),
        "42710" | "42723" | "42P07" => Problem::new(
            409,
            "conflict",
            "Something in the schema already has a name the extension needs.",
        ),
        "55P03" | "57014" => {
            Problem::unavailable("The database was busy and the change was not made. Try again.")
        }
        _ => {
            eprintln!("platform API extension change failed: {state}");
            Problem::new(
                500,
                "internal_error",
                "The server could not complete the request.",
            )
        }
    }
}
fn body_of(found: &Value) -> Value {
    let name = found["name"].as_str().unwrap_or_default();
    json!({
        "name": name,
        "description": found["description"],
        "default_version": found["default_version"],
        "enabled": !found["installed_version"].is_null(),
        "installed_version": found["installed_version"],
        "schema": found["schema"],
        "requires": found["requires"],
        "required": REQUIRED.contains(&name),
    })
}
fn one_sql(name: &str) -> Result<String, Problem> {
    Ok(format!(
        "SELECT {EXTENSION}::text FROM pg_available_extensions e WHERE e.name = {}::name AND {OFFERED}",
        literal(name)?
    ))
}
fn one(rows: &[Value]) -> Result<Reply, Problem> {
    let found: Value = rows
        .first()
        .and_then(|row| row.get(0)?.as_str())
        .and_then(|text| serde_json::from_str(text).ok())
        .ok_or_else(no_extension)?;
    Ok(Reply::ok(body_of(&found)))
}

pub fn list(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let limit = call.limit()?;
    let after = match call.cursor()? {
        None => String::new(),
        Some(position) => match <[String; 1]>::try_from(position) {
            Ok([name]) => format!(" AND e.name > {}::name", literal(&name)?),
            Err(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
        },
    };
    let sql = format!(
        "SELECT {EXTENSION}::text FROM pg_available_extensions e WHERE {OFFERED}{after} \
         ORDER BY e.name LIMIT {}",
        limit + 1
    );
    let rows = match read(store, host, call, at, sql, limit + 1)? {
        Ok(rows) => rows,
        Err(wait) => return Ok(wait),
    };
    let mut found: Vec<Value> = rows
        .iter()
        .filter_map(|row| serde_json::from_str::<Value>(row.get(0)?.as_str()?).ok())
        .map(|found| body_of(&found))
        .collect();
    let next = (found.len() > limit).then(|| {
        found.truncate(limit);
        cursor(&[found.last().unwrap()["name"].as_str().unwrap_or_default()])
    });
    Ok(page(found, next))
}

/// Stops a block for an extension the engine does not ship.
fn shipped(name: &str) -> Result<String, Problem> {
    Ok(format!(
        "IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = {}::name) THEN \
         RAISE EXCEPTION 'no such extension' USING ERRCODE = '42704'; END IF;",
        literal(name)?
    ))
}

pub fn enable(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    extension: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let name = named(extension)?;
    let schema = match &call.body {
        None => "public",
        Some(_) => text(fields(call, &["schema"])?, "schema")?.unwrap_or("public"),
    };
    let hidden = schema.starts_with("pg_")
        || [
            "information_schema",
            "_supabricks",
            "neon",
            "neon_migration",
        ]
        .contains(&schema);
    let within = identifier(schema)
        .ok()
        .filter(|_| !hidden)
        .ok_or_else(|| Problem::invalid("schema must name a schema of the project's own."))?;
    let write = || {
        let quoted = identifier(&name).map_err(|_| no_extension())?;
        let wanted = literal(&name)?;
        Ok(block(&[
            shipped(&name)?,
            // An extension that names its own schema goes there, and no other.
            format!(
                "IF (SELECT v.schema IS NULL FROM pg_available_extensions e \
                 JOIN pg_available_extension_versions v ON v.name = e.name AND v.version = e.default_version \
                 WHERE e.name = {wanted}::name) THEN \
                 CREATE EXTENSION IF NOT EXISTS {quoted} SCHEMA {within}; ELSE \
                 CREATE EXTENSION IF NOT EXISTS {quoted}; END IF;"
            ),
        ]))
    };
    match change_with(
        store,
        host,
        call,
        at,
        "",
        write,
        Some(one_sql(&name)?),
        refused,
    )? {
        Ok(rows) => one(&rows),
        Err(wait) => Ok(wait),
    }
}

pub fn disable(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    extension: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let name = named(extension)?;
    if REQUIRED.contains(&name.as_str()) {
        return Err(Problem::new(
            409,
            "conflict",
            "The engine needs this extension; it cannot be disabled.",
        ));
    }
    let write = || {
        let quoted = identifier(&name).map_err(|_| no_extension())?;
        Ok(block(&[
            shipped(&name)?,
            format!("DROP EXTENSION IF EXISTS {quoted};"),
        ]))
    };
    if let Err(wait) = change_with(store, host, call, at, "", write, None, refused)? {
        return Ok(wait);
    }
    Ok(Reply {
        status: 204,
        headers: BTreeMap::new(),
        body: None,
        wait: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn what_is_enabled_and_what_is_required() {
        let found = |name: &str, installed: Value| {
            body_of(
                &json!({"name": name, "description": "d", "default_version": "1.6",
                "installed_version": installed, "schema": null, "requires": []}),
            )
        };
        let off = found("pg_trgm", Value::Null);
        assert_eq!(
            (&off["enabled"], &off["required"]),
            (&json!(false), &json!(false))
        );
        let on = found("neon", json!("1.6"));
        assert_eq!(
            (&on["enabled"], &on["required"]),
            (&json!(true), &json!(true))
        );
        assert_eq!(found("plpgsql", json!("1.0"))["required"], true);
    }
    #[test]
    fn the_engines_own_tooling_is_not_offered() {
        for name in ["neon_utils", "neon_test_utils", "neon_rmgr", "a%00b", ""] {
            assert_eq!(named(name).unwrap_err().status, 404, "{name}");
        }
        assert_eq!(named("pg_trgm").unwrap(), "pg_trgm");
        assert_eq!(named("uuid-ossp").unwrap(), "uuid-ossp");
    }
    #[test]
    fn postgres_refusals_keep_their_meaning() {
        let error =
            |state: &str| json!({"code": "sql_error", "message": format!("{state}: detail")});
        for (state, status) in [
            ("42704", 404),
            ("58P01", 404),
            ("3F000", 404),
            ("2BP01", 409),
            ("42723", 409),
            ("55P03", 503),
            ("XX000", 500),
        ] {
            assert_eq!(refused(&error(state)).status, status, "{state}");
        }
    }
}
