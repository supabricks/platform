//! Tables of a branch: what exists, how one is built, and its rows a page at
//! a time. Each read is SQL the server writes itself and runs on a statement
//! worker; the reply says `wait` until PostgreSQL has answered, so the daemon
//! never waits on it.
use super::{
    Call, Local, Problem, Reply, branches, databases,
    shared::{cursor, page},
};
use crate::{
    console::workspace::{Target, identifier},
    query::{Query, WORKERS},
    store::Store,
};
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};
use supabricks_core::resource::{BranchId, OperationId, ProjectId};

/// Characters of one value a page of rows carries before it is cut.
const VALUE: usize = 2000;
const FILTERS: usize = 8;
const RELATIONS: &str = "c.relkind IN ('r','p','v','m','f')";
const HIDDEN: &str =
    "n.nspname NOT IN ('pg_catalog','information_schema','_supabricks') AND n.nspname !~ '^pg_'";

fn invalid(detail: impl Into<String>) -> Problem {
    Problem::invalid(detail)
}
fn no_table() -> Problem {
    Problem::not_found("There is no such table on this branch.")
}
/// A string literal that reads the same whatever the database's settings.
fn literal(text: &str) -> Result<String, Problem> {
    if text.contains('\0') {
        return Err(invalid("A value cannot contain a NUL character."));
    }
    Ok(format!(
        "E'{}'",
        text.replace('\\', "\\\\").replace('\'', "''")
    ))
}
/// A path segment as the name it spells.
fn segment(raw: &str) -> Result<String, Problem> {
    let name = percent_decode_str(raw)
        .decode_utf8()
        .map_err(|_| no_table())?;
    identifier(&name).map_err(|_| no_table())?;
    Ok(name.into_owned())
}
fn kind(relkind: &str) -> &'static str {
    match relkind {
        "r" => "table",
        "p" => "partitioned_table",
        "v" => "view",
        "m" => "materialized_view",
        _ => "foreign_table",
    }
}

// ---- Running a read ----

fn scope_of(runtime: ProjectId) -> String {
    format!("api-read:{runtime}")
}
fn lost() -> Problem {
    Problem::unavailable("The read was interrupted. Try again.")
}
/// Start a read-only statement and say which handle holds it.
fn start(
    store: &mut Store,
    host: &mut Option<Local>,
    runtime: ProjectId,
    branch: BranchId,
    sql: String,
    max_rows: usize,
) -> Result<OperationId, Problem> {
    if sql.len() > 32768 {
        return Err(Problem::new(
            422,
            "result_too_large",
            "The table has too many columns to read this way. Use a statement.",
        ));
    }
    let host = databases::owner(host)?;
    let (Some(cell), Some(gateway), Some(statements)) = (
        host.cell,
        host.gateway.as_deref_mut(),
        host.statements.as_deref_mut(),
    ) else {
        return Err(Problem::unavailable(
            "The database engines are not running.",
        ));
    };
    if store
        .governed_branch(branch)
        .map_err(|e| Problem::from(&e))?
    {
        return Err(Problem::new(
            409,
            "conflict",
            "PostgreSQL is private in this project; nobody reads its tables directly.",
        ));
    }
    store
        .accepting_work(branch)
        .map_err(|e| databases::refused("conflict", &e))?;
    if host.busy + statements.active() >= WORKERS {
        return Err(Problem::new(
            429,
            "capacity_exhausted",
            "The server is already running four statements. Try again when one finishes.",
        ));
    }
    gateway
        .ensure_listener(store, cell, branch)
        .map_err(|e| Problem::from(&e))?;
    let connection = store
        .stable_connection_json(runtime, branch)
        .map_err(|e| Problem::from(&e))?;
    let record = store.branch(branch).map_err(|e| Problem::from(&e))?;
    let id = OperationId::new();
    statements
        .start(
            scope_of(runtime),
            id,
            Target {
                branch,
                revision: record.revision,
            },
            Query {
                sql,
                read_only: true,
                max_rows,
                timeout_ms: 10000,
            },
            connection,
            store.generation(),
        )
        .map_err(|e| Problem::from(&e))?;
    Ok(id)
}
/// Why PostgreSQL refused a read the server wrote, in the caller's terms.
fn failed(error: &Value) -> Problem {
    let message = error["message"].as_str().unwrap_or_default();
    let (state, text) = match message.split_once(": ") {
        Some((state, text)) if error["code"] == "sql_error" => (state, text),
        _ => return Problem::unavailable("The database could not be reached. Try again."),
    };
    match state {
        // The table went away between two steps, or never was one.
        "42P01" | "3F000" => no_table(),
        "54000" => Problem::new(
            422,
            "result_too_large",
            "The page is larger than 256 KiB. Ask for fewer rows.",
        ),
        "57014" => Problem::unavailable("The database took too long to answer."),
        "42501" => Problem::new(
            409,
            "conflict",
            "The server's role may not read this table.",
        ),
        // A value that does not fit its column, or a comparison or ordering
        // the column's type does not have.
        state if state.starts_with("22") || matches!(state, "42883" | "42804" | "42846") => {
            invalid(format!("A filter or sort does not fit its column: {text}"))
        }
        _ => {
            eprintln!("platform API table read failed: {state}");
            Problem::new(
                500,
                "internal_error",
                "The server could not complete the request.",
            )
        }
    }
}
/// The rows a read returned, or `None` while it runs. `sql` is what this
/// request would have started, so a handle only answers the request it is for.
fn poll(
    host: &mut Option<Local>,
    runtime: ProjectId,
    id: &str,
    sql: &str,
) -> Result<Option<Vec<Value>>, Problem> {
    let statements = databases::owner(host)?
        .statements
        .as_deref_mut()
        .ok_or_else(lost)?;
    let scope = scope_of(runtime);
    let id: OperationId = id.parse().map_err(|_| lost())?;
    if statements
        .asked(&scope, id)
        .is_none_or(|(query, _)| query.sql != sql)
    {
        return Err(lost());
    }
    let value = statements.status(&scope, id, false).map_err(|_| lost())?;
    match value["state"].as_str() {
        Some("running" | "cancelling") => Ok(None),
        Some("succeeded") => {
            statements.forget(&scope, id);
            Ok(Some(
                value["result"]["rows"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default(),
            ))
        }
        _ => {
            statements.forget(&scope, id);
            Err(failed(&value["error"]))
        }
    }
}
/// Run one statement for a request: start it, then answer `wait` until done.
fn read(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    place: (ProjectId, BranchId),
    sql: String,
    max_rows: usize,
) -> Result<Result<Vec<Value>, Reply>, Problem> {
    let (runtime, branch) = place;
    match &call.resume {
        None => {
            let id = start(store, host, runtime, branch, sql, max_rows)?;
            Ok(Err(Reply::waiting(id.to_string())))
        }
        Some(id) => Ok(match poll(host, runtime, id, &sql)? {
            None => Err(Reply::waiting(id.clone())),
            Some(rows) => Ok(rows),
        }),
    }
}
fn place(store: &Store, path: (&str, &str, &str)) -> Result<(ProjectId, BranchId), Problem> {
    let (project, database, branch) = path;
    let (_, database, view) = branches::found(store, project, database, branch)?;
    Ok((database.project, view.id))
}

// ---- What exists ----

pub fn list(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let place = place(store, path)?;
    let limit = call.limit()?;
    let after = match call.cursor()? {
        None => None,
        Some(position) => match <[String; 2]>::try_from(position) {
            Ok(position) => Some(position),
            Err(_) => return Err(invalid("cursor is not one this server issued.")),
        },
    };
    let mut wanted = format!("{RELATIONS} AND {HIDDEN}");
    if let Some(schema) = call.parameter("schema")? {
        wanted += &format!(" AND n.nspname = {}", literal(schema)?);
    }
    if let Some([schema, name]) = &after {
        wanted += &format!(
            " AND (n.nspname, c.relname) > ({}::name, {}::name)",
            literal(schema)?,
            literal(name)?
        );
    }
    let sql = format!(
        "SELECT n.nspname, c.relname, c.relkind, \
         CASE WHEN c.relkind <> 'v' AND c.reltuples >= 0 THEN c.reltuples::bigint END, \
         CASE WHEN c.relkind IN ('r','m') THEN pg_total_relation_size(c.oid) END, \
         obj_description(c.oid, 'pg_class') \
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
         WHERE {wanted} ORDER BY n.nspname, c.relname LIMIT {}",
        limit + 1
    );
    let mut rows = match read(store, host, call, place, sql, limit + 1)? {
        Ok(rows) => rows,
        Err(wait) => return Ok(wait),
    };
    let next = (rows.len() > limit).then(|| {
        rows.truncate(limit);
        let last = rows.last().unwrap();
        cursor(&[
            last[0].as_str().unwrap_or_default(),
            last[1].as_str().unwrap_or_default(),
        ])
    });
    let number = |value: &Value| value.as_str().and_then(|n| n.parse::<i64>().ok());
    Ok(page(
        rows.iter()
            .map(|row| {
                json!({
                    "schema": row[0],
                    "name": row[1],
                    "kind": kind(row[2].as_str().unwrap_or_default()),
                    "row_estimate": number(&row[3]),
                    "size_bytes": number(&row[4]),
                    "comment": row[5],
                })
            })
            .collect(),
        next,
    ))
}

// ---- One table ----

/// One row, one column: the table as JSON, or no row when there is none.
fn describe(schema: &str, table: &str) -> Result<String, Problem> {
    Ok(format!(
        "SELECT json_build_object(\
         'schema', n.nspname, 'name', c.relname, 'kind', c.relkind, \
         'row_estimate', CASE WHEN c.relkind <> 'v' AND c.reltuples >= 0 THEN c.reltuples::bigint END, \
         'size_bytes', CASE WHEN c.relkind IN ('r','m') THEN pg_total_relation_size(c.oid) END, \
         'comment', obj_description(c.oid, 'pg_class'), \
         'columns', (SELECT coalesce(json_agg(json_build_object(\
           'name', a.attname, 'type', format_type(a.atttypid, a.atttypmod), 'nullable', NOT a.attnotnull, \
           'default', CASE WHEN a.attgenerated = '' THEN pg_get_expr(d.adbin, d.adrelid) END, \
           'generated', a.attgenerated <> '', 'identity', a.attidentity <> '', \
           'comment', col_description(c.oid, a.attnum)) ORDER BY a.attnum), '[]'::json) \
           FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum \
           WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), \
         'primary_key', (SELECT json_agg(a.attname ORDER BY k.ord) FROM pg_index i \
           CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) \
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum \
           WHERE i.indrelid = c.oid AND i.indisprimary), \
         'indexes', (SELECT coalesce(json_agg(json_build_object(\
           'name', ic.relname, 'unique', i.indisunique, 'primary', i.indisprimary, \
           'definition', pg_get_indexdef(i.indexrelid)) ORDER BY ic.relname), '[]'::json) \
           FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid WHERE i.indrelid = c.oid), \
         'constraints', (SELECT coalesce(json_agg(json_build_object(\
           'name', k.conname, 'kind', k.contype, 'definition', pg_get_constraintdef(k.oid)) ORDER BY k.conname), '[]'::json) \
           FROM pg_constraint k WHERE k.conrelid = c.oid)\
         )::text \
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
         WHERE {RELATIONS} AND {HIDDEN} AND n.nspname = {} AND c.relname = {}",
        literal(schema)?,
        literal(table)?
    ))
}
/// The contract's words for PostgreSQL's one-letter kinds.
fn described(rows: &[Value]) -> Result<Value, Problem> {
    let text = rows
        .first()
        .and_then(|row| row[0].as_str())
        .ok_or_else(no_table)?;
    let mut table: Value = serde_json::from_str(text).map_err(|_| {
        Problem::new(
            500,
            "internal_error",
            "The table description is unreadable.",
        )
    })?;
    table["kind"] = json!(kind(table["kind"].as_str().unwrap_or_default()));
    for constraint in table["constraints"].as_array_mut().into_iter().flatten() {
        constraint["kind"] = json!(match constraint["kind"].as_str() {
            Some("p") => "primary_key",
            Some("u") => "unique",
            Some("f") => "foreign_key",
            Some("c") => "check",
            Some("x") => "exclusion",
            Some("n") => "not_null",
            _ => "other",
        });
    }
    Ok(table)
}

pub fn get(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    table: (&str, &str),
) -> Result<Reply, Problem> {
    let place = place(store, path)?;
    let sql = describe(&segment(table.0)?, &segment(table.1)?)?;
    match read(store, host, call, place, sql, 1)? {
        Ok(rows) => Ok(Reply::ok(described(&rows)?)),
        Err(wait) => Ok(wait),
    }
}

// ---- Rows ----

struct Asked {
    limit: usize,
    offset: usize,
    sort: Option<String>,
    descending: bool,
    filters: Vec<(String, String, Option<String>)>,
    /// What a cursor must have been issued for, to continue this listing.
    listing: String,
}
fn asked(call: &Call) -> Result<Asked, Problem> {
    let limit = call.limit()?;
    let sort = call.parameter("sort")?.map(str::to_owned);
    let descending = match call.parameter("order")? {
        None | Some("asc") => false,
        Some("desc") => true,
        Some(_) => return Err(invalid("order must be asc or desc.")),
    };
    let raw = call.parameter("filter")?.unwrap_or("[]");
    let bad = || {
        invalid(
            "filter must be a JSON array of at most 8 objects with column, op and, except for \
             is_null and not_null, a text value of at most 256 characters.",
        )
    };
    let parsed: Vec<Value> = serde_json::from_str(raw).map_err(|_| bad())?;
    if parsed.len() > FILTERS {
        return Err(bad());
    }
    let mut filters = Vec::new();
    for filter in &parsed {
        let object = filter.as_object().ok_or_else(bad)?;
        if object
            .keys()
            .any(|k| !matches!(k.as_str(), "column" | "op" | "value"))
        {
            return Err(bad());
        }
        let column = object
            .get("column")
            .and_then(Value::as_str)
            .ok_or_else(bad)?;
        let op = object.get("op").and_then(Value::as_str).ok_or_else(bad)?;
        let value = match (op, object.get("value")) {
            ("is_null" | "not_null", None) => None,
            (
                "eq" | "ne" | "lt" | "lte" | "gt" | "gte" | "contains",
                Some(Value::String(value)),
            ) if value.chars().count() <= 256 => Some(value.clone()),
            _ => return Err(bad()),
        };
        filters.push((column.to_owned(), op.to_owned(), value));
    }
    let listing = json!([sort, descending, parsed]).to_string();
    let offset = match call.cursor()? {
        None => 0,
        Some(position) => match position.as_slice() {
            [offset, issued] if *issued == listing => offset
                .parse()
                .map_err(|_| invalid("cursor is not one this server issued."))?,
            _ => {
                return Err(invalid(
                    "cursor is not one this server issued for this sort and filter.",
                ));
            }
        },
    };
    Ok(Asked {
        limit,
        offset,
        sort,
        descending,
        filters,
        listing,
    })
}
/// The statement for one page, written from the table's own description.
fn select(schema: &str, table: &Value, asked: &Asked) -> Result<String, Problem> {
    let columns: Vec<&str> = table["columns"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|c| c["name"].as_str())
        .collect();
    let name = |column: &str| {
        if !columns.contains(&column) {
            return Err(invalid(format!("The table has no column named {column}.")));
        }
        identifier(column).map_err(|_| invalid("A column name is not valid."))
    };
    let quoted = |text: &str| identifier(text).map_err(|_| no_table());
    let values: Vec<String> = columns
        .iter()
        .map(|c| Ok(format!("left({}::text, {})", quoted(c)?, VALUE + 1)))
        .collect::<Result<_, Problem>>()?;
    let mut conditions = Vec::new();
    for (column, op, value) in &asked.filters {
        let column = name(column)?;
        let value = value.as_deref().unwrap_or_default();
        conditions.push(match op.as_str() {
            "is_null" => format!("{column} IS NULL"),
            "not_null" => format!("{column} IS NOT NULL"),
            "contains" => {
                let pattern = value
                    .replace('\\', "\\\\")
                    .replace('%', "\\%")
                    .replace('_', "\\_");
                format!("{column}::text ILIKE {}", literal(&format!("%{pattern}%"))?)
            }
            op => {
                let sign = match op {
                    "eq" => "=",
                    "ne" => "<>",
                    "lt" => "<",
                    "lte" => "<=",
                    "gt" => ">",
                    _ => ">=",
                };
                format!("{column} {sign} {}", literal(value)?)
            }
        });
    }
    let mut order = Vec::new();
    if let Some(sort) = &asked.sort {
        let direction = if asked.descending { "DESC" } else { "ASC" };
        order.push(format!("{} {direction}", name(sort)?));
    }
    // After the caller's order, an order of the server's own, so that pages
    // do not overlap while nothing is written.
    match (table["primary_key"].as_array(), table["kind"].as_str()) {
        (Some(key), _) => {
            for column in key.iter().filter_map(Value::as_str) {
                order.push(quoted(column)?);
            }
        }
        (None, Some("table" | "materialized_view")) => order.push("ctid".into()),
        (None, Some("partitioned_table")) => order.push("tableoid, ctid".into()),
        _ => {}
    }
    let mut sql = format!(
        "SELECT {} FROM {}.{}",
        values.join(", "),
        quoted(schema)?,
        quoted(table["name"].as_str().unwrap_or_default())?
    );
    if !conditions.is_empty() {
        sql += &format!(" WHERE {}", conditions.join(" AND "));
    }
    if !order.is_empty() {
        sql += &format!(" ORDER BY {}", order.join(", "));
    }
    Ok(sql + &format!(" LIMIT {} OFFSET {}", asked.limit + 1, asked.offset))
}

pub fn rows(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    table: (&str, &str),
) -> Result<Reply, Problem> {
    let (runtime, branch) = place(store, path)?;
    let (schema, name) = (segment(table.0)?, segment(table.1)?);
    let asked = asked(call)?;
    let describe = describe(&schema, &name)?;
    // Two statements: what the table is, then the page. The token carries the
    // description from the first to the second.
    let (step, id, carried) = match call.resume.as_deref().map(|t| t.splitn(3, ':')) {
        None => {
            let id = start(store, host, runtime, branch, describe, 1)?;
            return Ok(Reply::waiting(format!("table:{id}")));
        }
        Some(mut parts) => (
            parts.next().unwrap_or_default(),
            parts.next().unwrap_or_default(),
            parts.next(),
        ),
    };
    let table = match (step, carried) {
        ("table", _) => {
            let Some(found) = poll(host, runtime, id, &describe)? else {
                return Ok(Reply::waiting(format!("table:{id}")));
            };
            let table = described(&found)?;
            let sql = select(&schema, &table, &asked)?;
            let id = start(store, host, runtime, branch, sql, asked.limit + 1)?;
            return Ok(Reply::waiting(format!(
                "rows:{id}:{}",
                hex::encode(table.to_string())
            )));
        }
        ("rows", Some(carried)) => hex::decode(carried)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .ok_or_else(lost)?,
        _ => return Err(lost()),
    };
    let sql = select(&schema, &table, &asked)?;
    let Some(mut found) = poll(host, runtime, id, &sql)? else {
        return Ok(Reply::waiting(call.resume.clone().unwrap_or_default()));
    };
    let next = (found.len() > asked.limit).then(|| {
        found.truncate(asked.limit);
        cursor(&[&(asked.offset + asked.limit).to_string(), &asked.listing])
    });
    let mut truncated = Vec::new();
    for (row, values) in found.iter_mut().enumerate() {
        for (column, value) in values.as_array_mut().into_iter().flatten().enumerate() {
            if let Some(text) = value.as_str()
                && text.chars().count() > VALUE
            {
                *value = json!(text.chars().take(VALUE).collect::<String>());
                truncated.push(json!([row, column]));
            }
        }
    }
    let columns: Vec<Value> = table["columns"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|c| json!({"name": c["name"], "type": c["type"]}))
        .collect();
    Ok(Reply::ok(json!({
        "columns": columns,
        "primary_key": table["primary_key"],
        "items": found,
        "truncated": truncated,
        "next_cursor": next,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(query: &[(&str, &str)]) -> Call {
        Call {
            caller: crate::rest::Caller::LocalOwner,
            method: "GET".into(),
            path: "/".into(),
            query: query
                .iter()
                .map(|(n, v)| ((*n).to_owned(), (*v).to_owned()))
                .collect(),
            idempotency_key: None,
            if_match: None,
            body: None,
            resume: None,
            part: None,
        }
    }
    fn table(kind: &str, key: Value) -> Value {
        json!({"name": "Order \"Items\"", "kind": kind, "primary_key": key, "columns": [
            {"name": "id", "type": "bigint"}, {"name": "note's", "type": "text"}]})
    }

    #[test]
    fn a_literal_reads_the_same_under_either_string_setting() {
        assert_eq!(literal("it's").unwrap(), "E'it''s'");
        assert_eq!(literal(r"a\'; drop").unwrap(), r"E'a\\''; drop'");
        assert_eq!(literal("").unwrap(), "E''");
        assert_eq!(literal("a\0b").unwrap_err().status, 400);
    }

    #[test]
    fn a_page_is_ordered_by_what_was_asked_and_then_by_the_key() {
        let filter = r#"[{"column":"note's","op":"contains","value":"50%_\\"},{"column":"id","op":"gte","value":"7"},{"column":"id","op":"not_null"}]"#;
        let asked = asked(&call(&[
            ("sort", "note's"),
            ("order", "desc"),
            ("filter", filter),
            ("limit", "2"),
        ]))
        .unwrap();
        let sql = select("s", &table("table", json!(["id"])), &asked).unwrap();
        println!("SQL> {sql}");
        assert_eq!(
            sql,
            "SELECT left(\"id\"::text, 2001), left(\"note's\"::text, 2001) FROM \"s\".\"Order \"\"Items\"\"\" \
             WHERE \"note's\"::text ILIKE E'%50\\\\%\\\\_\\\\\\\\%' AND \"id\" >= E'7' AND \"id\" IS NOT NULL \
             ORDER BY \"note's\" DESC, \"id\" LIMIT 3 OFFSET 0"
        );
    }

    #[test]
    fn a_table_without_a_key_is_ordered_by_where_its_rows_are() {
        let plain = asked(&call(&[])).unwrap();
        let order = |kind: &str| {
            let sql = select("s", &table(kind, Value::Null), &plain).unwrap();
            println!("SQL> {sql}");
            sql.split_once(" ORDER BY ")
                .map(|(_, order)| order.to_owned())
        };
        assert_eq!(order("table").as_deref(), Some("ctid LIMIT 51 OFFSET 0"));
        assert_eq!(
            order("partitioned_table").as_deref(),
            Some("tableoid, ctid LIMIT 51 OFFSET 0")
        );
        assert_eq!(order("view"), None);
        let empty = json!({"name": "t", "kind": "table", "primary_key": null, "columns": []});
        assert!(
            select("s", &empty, &plain)
                .unwrap()
                .starts_with("SELECT  FROM")
        );
    }

    #[test]
    fn a_sort_or_filter_names_a_column_of_the_table() {
        let table = table("table", Value::Null);
        for query in [
            vec![("sort", "missing")],
            vec![("filter", r#"[{"column":"missing","op":"is_null"}]"#)],
        ] {
            let asked = asked(&call(&query)).unwrap();
            assert_eq!(select("s", &table, &asked).unwrap_err().status, 400);
        }
    }

    #[test]
    fn a_filter_is_checked_before_anything_runs() {
        for bad in [
            "{}",
            "x",
            r#"[1]"#,
            r#"[{"column":"a"}]"#,
            r#"[{"column":"a","op":"like","value":"x"}]"#,
            r#"[{"column":"a","op":"eq"}]"#,
            r#"[{"column":"a","op":"eq","value":1}]"#,
            r#"[{"column":"a","op":"eq","value":null}]"#,
            r#"[{"column":"a","op":"is_null","value":"x"}]"#,
            r#"[{"column":"a","op":"eq","value":"x","extra":1}]"#,
            r#"[{"column":1,"op":"is_null"}]"#,
        ] {
            assert_eq!(
                asked(&call(&[("filter", bad)])).err().map(|p| p.status),
                Some(400),
                "{bad}"
            );
        }
        let nine = format!("[{}]", [r#"{"column":"a","op":"is_null"}"#; 9].join(","));
        assert!(asked(&call(&[("filter", &nine)])).is_err());
        assert!(asked(&call(&[("order", "down")])).is_err());
    }

    #[test]
    fn a_cursor_continues_only_the_listing_it_was_issued_for() {
        let first = asked(&call(&[("sort", "id")])).unwrap();
        let next = cursor(&["50", &first.listing]);
        assert_eq!(
            asked(&call(&[("sort", "id"), ("cursor", &next)]))
                .unwrap()
                .offset,
            50
        );
        for other in [
            vec![("cursor", next.as_str())],
            vec![("sort", "id"), ("order", "desc"), ("cursor", &next)],
        ] {
            assert_eq!(asked(&call(&other)).err().map(|p| p.status), Some(400));
        }
    }

    #[test]
    fn a_refusal_from_postgres_is_put_in_the_callers_terms() {
        let state = |state: &str| {
            let problem =
                failed(&json!({"code": "sql_error", "message": format!("{state}: detail")}));
            (problem.status, problem.code)
        };
        assert_eq!(state("42P01"), (404, "not_found"));
        assert_eq!(state("54000"), (422, "result_too_large"));
        assert_eq!(state("22P02"), (400, "invalid_request"));
        assert_eq!(state("42883"), (400, "invalid_request"));
        assert_eq!(state("57014"), (503, "unavailable"));
        assert_eq!(state("XX000"), (500, "internal_error"));
        assert_eq!(
            failed(&json!({"code": "unavailable", "message": "x"})).status,
            503
        );
    }

    #[test]
    fn the_statements_the_server_writes_can_be_printed_for_checking() {
        println!("SQL> {}", describe("public", "Order \"Items\"").unwrap());
    }
}
