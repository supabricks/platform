//! Roles of a branch: who can log in to its PostgreSQL and what they may do.
//! Roles are data of the branch itself; the daemon keeps nothing about them.
//! Reads run as the application role. Changes run as the engine's control
//! role, because the application role may not create roles, in one
//! transaction each, with SQL the server writes.
use super::{
    Call, Local, PREFIX, Problem, Reply,
    shared::{cursor, fields, page, text},
    tables::{As, literal, lost, place, poll_as, start_as},
};
use crate::{console::workspace::identifier, store::Store};
use percent_encoding::percent_decode_str;
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use supabricks_core::{
    keys::pg_md5,
    resource::{BranchId, OperationId, ProjectId},
};

/// The role applications connect as. The platform owns its password.
const OWNER: &str = "supabricks_owner";
/// Membership in these is what an access level means.
const READ: &str = "pg_read_all_data";
const WRITE: &str = "pg_write_all_data";
/// What a role may be granted on the tables of a schema.
const TABLE: [&str; 7] = [
    "select",
    "insert",
    "update",
    "delete",
    "truncate",
    "references",
    "trigger",
];
/// Schemas that are the engine's or PostgreSQL's own.
const HIDDEN: &str = "n.nspname !~ '^pg_' AND n.nspname NOT IN \
    ('information_schema','_supabricks','neon','neon_migration')";
const KINDS: &str = "('r','p','v','m','f')";
/// A role as one JSON object, from `pg_roles r`.
const ROLE: &str = "json_build_object('name', r.rolname, 'login', r.rolcanlogin, \
    'superuser', r.rolsuper, 'create_role', r.rolcreaterole, 'create_database', r.rolcreatedb, \
    'replication', r.rolreplication, 'bypass_rls', r.rolbypassrls, \
    'connection_limit', r.rolconnlimit, \
    'member_of', coalesce((SELECT json_agg(g.rolname ORDER BY g.rolname) FROM pg_auth_members m \
        JOIN pg_roles g ON g.oid = m.roleid WHERE m.member = r.oid), '[]'::json), \
    'connections', (SELECT count(*) FROM pg_stat_activity a WHERE a.usesysid = r.oid))";

fn no_role() -> Problem {
    Problem::not_found("There is no such role on this branch.")
}
fn no_schema() -> Problem {
    Problem::not_found("There is no such schema on this branch.")
}
fn managed_role() -> Problem {
    Problem::new(
        409,
        "conflict",
        "The engine manages this role; it cannot be changed here.",
    )
}
/// Whether the engine or PostgreSQL made the role, by its name alone.
fn reserved(name: &str) -> bool {
    ["pg_", "sb_", "supabricks_", "neon", "cloud_"]
        .iter()
        .any(|prefix| name.starts_with(prefix))
}
/// A role named in a path, as the name it spells.
fn named(raw: &str) -> Result<String, Problem> {
    let name = percent_decode_str(raw)
        .decode_utf8()
        .map_err(|_| no_role())?;
    identifier(&name).map_err(|_| no_role())?;
    Ok(name.into_owned())
}
/// A role in a path that a request means to change.
fn changeable(raw: &str) -> Result<String, Problem> {
    let name = named(raw)?;
    if reserved(&name) {
        return Err(managed_role());
    }
    Ok(name)
}
fn quoted(name: &str) -> Result<String, Problem> {
    identifier(name).map_err(|_| no_role())
}
fn secret() -> String {
    format!("{}{}", OperationId::new(), OperationId::new())
}
/// A password as PostgreSQL stores it, so the statement never holds it.
fn password_of(name: &str, password: &str) -> String {
    format!("'md5{}'", pg_md5(password, name))
}
/// One transaction of several commands, quoted with a tag no request can
/// know, so no name or value can close the block.
fn block(commands: &[String]) -> String {
    let tag = format!("sb{}", OperationId::new().to_string().replace('-', ""));
    format!("DO ${tag}$ BEGIN {} END ${tag}$", commands.join(" "))
}
/// Commands that stop a block for a role that is missing or the engine's.
fn guard(name: &str) -> Result<Vec<String>, Problem> {
    let role = literal(name)?;
    Ok(vec![
        format!(
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role}::name) THEN \
             RAISE EXCEPTION 'no such role' USING ERRCODE = '42704'; END IF;"
        ),
        format!(
            "IF (SELECT rolsuper FROM pg_roles WHERE rolname = {role}::name) THEN \
             RAISE EXCEPTION 'managed role' USING ERRCODE = 'SB001'; END IF;"
        ),
    ])
}
/// Why PostgreSQL refused a change, in the caller's terms.
fn refused(error: &Value) -> Problem {
    let message = error["message"].as_str().unwrap_or_default();
    let state = match message.split_once(": ") {
        Some((state, _)) if error["code"] == "sql_error" => state,
        _ => return Problem::unavailable("The database could not be reached. Try again."),
    };
    match state {
        "42710" => Problem::new(
            409,
            "name_taken",
            "The branch already has a role of that name.",
        ),
        "42704" => no_role(),
        "3F000" => no_schema(),
        "SB001" => managed_role(),
        "2BP01" => Problem::new(
            409,
            "conflict",
            "Something in the database still depends on the role.",
        ),
        "55P03" | "57014" => {
            Problem::unavailable("The database was busy and the change was not made. Try again.")
        }
        _ => {
            eprintln!("platform API role change failed: {state}");
            Problem::new(
                500,
                "internal_error",
                "The server could not complete the request.",
            )
        }
    }
}

// ---- Running statements across the calls of one request ----

/// Run a read for a request: start it, then answer `wait` until it is done.
fn read(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    at: (ProjectId, BranchId),
    sql: String,
    max_rows: usize,
) -> Result<Result<Vec<Value>, Reply>, Problem> {
    let (runtime, branch) = at;
    let Some(id) = &call.resume else {
        let id = start_as(store, host, runtime, branch, sql, max_rows, As::Reader)?;
        return Ok(Err(Reply::waiting(id.to_string())));
    };
    Ok(match poll_as(host, runtime, id, Some(&sql), As::Reader)? {
        None => Err(Reply::waiting(id.clone())),
        Some(Ok(rows)) => Ok(rows),
        Some(Err(_)) => {
            return Err(Problem::unavailable(
                "The database could not be reached. Try again.",
            ));
        }
    })
}
/// What a request that changes something carries from call to call: the step
/// it is at, the statement that step waits on, and a password it made.
struct Progress<'a> {
    step: &'a str,
    handle: &'a str,
    secret: &'a str,
}
impl<'a> Progress<'a> {
    fn of(call: &'a Call) -> Result<Option<Self>, Problem> {
        let Some(token) = call.resume.as_deref() else {
            return Ok(None);
        };
        let mut parts = token.splitn(3, ':');
        match (parts.next(), parts.next(), parts.next()) {
            (Some(step), Some(handle), Some(secret)) => Ok(Some(Self {
                step,
                handle,
                secret,
            })),
            _ => Err(lost()),
        }
    }
}
fn waiting(step: &str, handle: &str, secret: &str) -> Reply {
    Reply::waiting(format!("{step}:{handle}:{secret}"))
}
/// Make a change, then optionally read what it left. `write` is started once;
/// the calls the transport repeats only follow it. A read that cannot start
/// because the workers are busy is tried again, not reported: the change is
/// already made.
fn change(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    at: (ProjectId, BranchId),
    secret: &str,
    write: impl FnOnce() -> Result<String, Problem>,
    after: Option<String>,
) -> Result<Result<Vec<Value>, Reply>, Problem> {
    let (runtime, branch) = at;
    let Some(progress) = Progress::of(call)? else {
        let id = start_as(store, host, runtime, branch, write()?, 1, As::Control)?;
        return Ok(Err(waiting("w", &id.to_string(), secret)));
    };
    let secret = progress.secret;
    let written = match progress.step {
        "w" => match poll_as(host, runtime, progress.handle, None, As::Control)? {
            None => return Ok(Err(waiting("w", progress.handle, secret))),
            Some(Err(error)) => return Err(refused(&error)),
            Some(Ok(_)) => true,
        },
        "p" => true,
        _ => false,
    };
    if written {
        let Some(sql) = after else {
            return Ok(Ok(Vec::new()));
        };
        return Ok(Err(
            match start_as(store, host, runtime, branch, sql, 1, As::Reader) {
                Ok(id) => waiting("r", &id.to_string(), secret),
                Err(problem) if problem.status == 429 => waiting("p", "-", secret),
                Err(problem) => return Err(problem),
            },
        ));
    }
    Ok(
        match poll_as(host, runtime, progress.handle, None, As::Reader)? {
            None => Err(waiting("r", progress.handle, secret)),
            Some(Ok(rows)) => Ok(rows),
            Some(Err(_)) => {
                return Err(Problem::unavailable(
                    "The change was made, but could not be read back. Read the role.",
                ));
            }
        },
    )
}
/// The password a request made on its first call.
fn made(call: &Call) -> Result<String, Problem> {
    Ok(match Progress::of(call)? {
        Some(progress) => progress.secret.to_owned(),
        None => secret(),
    })
}

// ---- What a role is ----

fn access_of(members: &[&str]) -> &'static str {
    let holds = |role: &str| members.contains(&role);
    match (holds(OWNER), holds(READ), holds(WRITE), members.len()) {
        (false, false, false, 0) => "none",
        (false, true, false, 1) => "read",
        (false, true, true, 2) => "read_write",
        (true, _, _, _) => "owner",
        _ => "custom",
    }
}
fn memberships(access: &str) -> Result<&'static [&'static str], Problem> {
    Ok(match access {
        "none" => &[],
        "read" => &[READ],
        "read_write" => &[READ, WRITE],
        "owner" => &[OWNER],
        _ => {
            return Err(Problem::invalid(
                "access must be none, read, read_write or owner.",
            ));
        }
    })
}
/// The contract's role, from the object the read returns.
fn role_body(found: &Value) -> Value {
    let name = found["name"].as_str().unwrap_or_default();
    let managed = reserved(name) || found["superuser"] == true;
    let members: Vec<&str> = found["member_of"]
        .as_array()
        .map(|list| list.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    json!({
        "name": name,
        "managed": managed,
        "login": found["login"],
        "access": if managed { Value::Null } else { json!(access_of(&members)) },
        "member_of": members,
        "superuser": found["superuser"],
        "create_role": found["create_role"],
        "create_database": found["create_database"],
        "replication": found["replication"],
        "bypass_rls": found["bypass_rls"],
        "connection_limit": found["connection_limit"].as_i64().filter(|limit| *limit >= 0),
        "connections": found["connections"],
    })
}
/// One JSON array of what a role holds in each schema, or in one.
fn privileges_sql(role: &str, schema: Option<&str>) -> Result<String, Problem> {
    let role = literal(role)?;
    let only = match schema {
        None => String::new(),
        Some(schema) => format!(" AND n.nspname = {}::name", literal(schema)?),
    };
    Ok(format!(
        "SELECT coalesce(json_agg(json_build_object('schema', n.nspname, \
         'usage', EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a WHERE a.grantee = r.oid AND a.privilege_type = 'USAGE'), \
         'create', EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a WHERE a.grantee = r.oid AND a.privilege_type = 'CREATE'), \
         'table_count', (SELECT count(*) FROM pg_class c WHERE c.relnamespace = n.oid AND c.relkind IN {KINDS}), \
         'held', (SELECT coalesce(json_object_agg(p.privilege_type, p.tables), '{{}}'::json) FROM \
            (SELECT a.privilege_type, count(*) AS tables FROM pg_class c, aclexplode(c.relacl) a \
             WHERE c.relnamespace = n.oid AND c.relkind IN {KINDS} AND a.grantee = r.oid \
             GROUP BY a.privilege_type) p), \
         'future', (SELECT coalesce(json_agg(DISTINCT a.privilege_type), '[]'::json) \
            FROM pg_default_acl d, aclexplode(d.defaclacl) a \
            WHERE d.defaclnamespace = n.oid AND d.defaclobjtype = 'r' AND a.grantee = r.oid \
            AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = '{OWNER}'))) \
         ORDER BY n.nspname), '[]'::json) \
         FROM pg_namespace n, pg_roles r WHERE r.rolname = {role}::name AND {HIDDEN}{only}"
    ))
}
/// The contract's privileges of one schema, from the object the read returns.
fn privileges_body(found: &Value) -> Value {
    let tables = found["table_count"].as_u64().unwrap_or_default();
    let held = |privilege: &str| {
        found["held"][privilege.to_uppercase()]
            .as_u64()
            .unwrap_or(0)
    };
    let future: Vec<&str> = TABLE
        .into_iter()
        .filter(|privilege| {
            found["future"]
                .as_array()
                .is_some_and(|list| list.iter().any(|p| p == &json!(privilege.to_uppercase())))
        })
        .collect();
    let on_all: Vec<&str> = TABLE
        .into_iter()
        .filter(|privilege| tables > 0 && held(privilege) == tables)
        .collect();
    json!({
        "schema": found["schema"],
        "usage": found["usage"],
        "create": found["create"],
        "tables": on_all,
        "future_tables": future,
        "uniform": TABLE.into_iter().all(|privilege| [0, tables].contains(&held(privilege))),
    })
}
fn parsed(rows: &[Value], column: usize) -> Option<Value> {
    serde_json::from_str(rows.first()?.get(column)?.as_str()?).ok()
}

// ---- Reads ----

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
            Ok([name]) => format!(" WHERE r.rolname > {}::name", literal(&name)?),
            Err(_) => return Err(Problem::invalid("cursor is not one this server issued.")),
        },
    };
    let sql = format!(
        "SELECT {ROLE}::text FROM pg_roles r{after} ORDER BY r.rolname LIMIT {}",
        limit + 1
    );
    let rows = match read(store, host, call, at, sql, limit + 1)? {
        Ok(rows) => rows,
        Err(wait) => return Ok(wait),
    };
    let mut roles: Vec<Value> = rows
        .iter()
        .filter_map(|row| serde_json::from_str::<Value>(row.get(0)?.as_str()?).ok())
        .map(|found| role_body(&found))
        .collect();
    let next = (roles.len() > limit).then(|| {
        roles.truncate(limit);
        cursor(&[roles.last().unwrap()["name"].as_str().unwrap_or_default()])
    });
    Ok(page(roles, next))
}

/// A role with what it holds in each schema, as one row of two JSON texts.
fn role_sql(name: &str) -> Result<String, Problem> {
    Ok(format!(
        "SELECT {ROLE}::text, ({})::text FROM pg_roles r WHERE r.rolname = {}::name",
        privileges_sql(name, None)?,
        literal(name)?
    ))
}
fn role_reply(rows: &[Value]) -> Result<Reply, Problem> {
    let found = parsed(rows, 0).ok_or_else(no_role)?;
    let mut body = role_body(&found);
    body["privileges"] = parsed(rows, 1)
        .as_ref()
        .and_then(Value::as_array)
        .map(|list| list.iter().map(privileges_body).collect())
        .unwrap_or_else(|| json!([]));
    Ok(Reply::ok(body))
}

pub fn get(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    role: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let sql = role_sql(&named(role)?)?;
    match read(store, host, call, at, sql, 1)? {
        Ok(rows) => role_reply(&rows),
        Err(wait) => Ok(wait),
    }
}

// ---- Changes ----

/// The address of a branch for a role's connection string.
fn uri(
    store: &Store,
    at: (ProjectId, BranchId),
    name: &str,
    password: &str,
) -> Result<String, Problem> {
    let found = store
        .stable_connection_json(at.0, at.1)
        .map_err(|e| Problem::from(&e))?;
    Ok(format!(
        "postgresql://{name}:{password}@127.0.0.1:{}/postgres?sslmode=prefer",
        found["port"]
    ))
}
fn shown(mut reply: Reply) -> Reply {
    reply
        .headers
        .insert("Cache-Control".into(), "no-store".into());
    reply
}

pub fn create(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
) -> Result<Reply, Problem> {
    let (project, database, branch) = path;
    let at = place(store, path)?;
    let body = fields(call, &["name", "access"])?;
    let name = text(body, "name")?.unwrap_or_default();
    let plain = name.len() <= 63
        && name
            .bytes()
            .enumerate()
            .all(|(i, b)| b.is_ascii_lowercase() || b == b'_' || (i > 0 && b.is_ascii_digit()));
    if name.is_empty() || !plain || reserved(name) {
        return Err(Problem::invalid(
            "name must be 1 to 63 lower-case letters, digits and underscores, not start with a \
             digit, and not start with pg_, sb_, supabricks_, neon or cloud_.",
        ));
    }
    let access = text(body, "access")?.unwrap_or("none");
    let members = memberships(access)?;
    let password = made(call)?;
    let write = || {
        let role = quoted(name)?;
        let mut commands = vec![format!(
            "CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION \
             NOBYPASSRLS PASSWORD {};",
            password_of(name, &password)
        )];
        for member in members {
            commands.push(format!("GRANT {member} TO {role};"));
        }
        Ok(block(&commands))
    };
    if let Err(wait) = change(store, host, call, at, &password, write, None)? {
        return Ok(wait);
    }
    let mut made = role_body(&json!({
        "name": name, "login": true, "superuser": false, "create_role": false,
        "create_database": false, "replication": false, "bypass_rls": false,
        "connection_limit": -1, "member_of": members, "connections": 0,
    }));
    made["uri"] = json!(uri(store, at, name, &password)?);
    made["password"] = json!(password);
    let mut reply = shown(Reply::ok(made));
    reply.status = 201;
    reply.headers.insert(
        "Location".into(),
        format!("{PREFIX}/projects/{project}/databases/{database}/branches/{branch}/roles/{name}"),
    );
    Ok(reply)
}

pub fn update(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    role: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let name = changeable(role)?;
    let body: &Map<String, Value> = fields(call, &["login", "access"])?;
    let login = match body.get("login") {
        None => None,
        Some(Value::Bool(login)) => Some(*login),
        Some(_) => return Err(Problem::invalid("login must be true or false.")),
    };
    let members = text(body, "access")?.map(memberships).transpose()?;
    if login.is_none() && members.is_none() {
        return Err(Problem::invalid("Send login, access or both."));
    }
    let write = || {
        let role = quoted(&name)?;
        let mut commands = guard(&name)?;
        if let Some(login) = login {
            let word = if login { "LOGIN" } else { "NOLOGIN" };
            commands.push(format!("ALTER ROLE {role} {word};"));
        }
        if let Some(members) = members {
            commands.push(format!("REVOKE {READ}, {WRITE}, {OWNER} FROM {role};"));
            for member in members {
                commands.push(format!("GRANT {member} TO {role};"));
            }
        }
        Ok(block(&commands))
    };
    match change(store, host, call, at, "", write, Some(role_sql(&name)?))? {
        Ok(rows) => role_reply(&rows),
        Err(wait) => Ok(wait),
    }
}

pub fn delete(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    role: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let name = changeable(role)?;
    let write = || {
        let role = quoted(&name)?;
        let mut commands = guard(&name)?;
        commands.extend([
            // What the role made stays, under the application role.
            format!("REASSIGN OWNED BY {role} TO {OWNER};"),
            format!("DROP OWNED BY {role};"),
            format!(
                "PERFORM pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = {}::name;",
                literal(&name)?
            ),
            format!("DROP ROLE {role};"),
        ]);
        Ok(block(&commands))
    };
    if let Err(wait) = change(store, host, call, at, "", write, None)? {
        return Ok(wait);
    }
    Ok(Reply {
        status: 204,
        headers: BTreeMap::new(),
        body: None,
        wait: None,
    })
}

pub fn reset_password(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    role: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    if named(role)? == OWNER {
        return Err(Problem::new(
            409,
            "conflict",
            "The platform keeps the application role's password. Reset the database's credentials.",
        ));
    }
    let name = changeable(role)?;
    if call.body.is_some() {
        fields(call, &[])?;
    }
    let password = made(call)?;
    let write = || {
        let mut commands = guard(&name)?;
        commands.push(format!(
            "ALTER ROLE {} PASSWORD {};",
            quoted(&name)?,
            password_of(&name, &password)
        ));
        Ok(block(&commands))
    };
    if let Err(wait) = change(store, host, call, at, &password, write, None)? {
        return Ok(wait);
    }
    Ok(shown(Reply::ok(json!({
        "role": name,
        "password": password,
        "uri": uri(store, at, &name, &password)?,
        "expires_at": null,
    }))))
}

pub fn set_privileges(
    store: &mut Store,
    host: &mut Option<Local>,
    call: &Call,
    path: (&str, &str, &str),
    role: &str,
    schema: &str,
) -> Result<Reply, Problem> {
    let at = place(store, path)?;
    let name = changeable(role)?;
    let schema = percent_decode_str(schema)
        .decode_utf8()
        .map_err(|_| no_schema())?
        .into_owned();
    let hidden = schema.starts_with("pg_")
        || [
            "information_schema",
            "_supabricks",
            "neon",
            "neon_migration",
        ]
        .contains(&schema.as_str());
    let within = identifier(&schema).map_err(|_| no_schema())?;
    if hidden {
        return Err(no_schema());
    }
    let body = fields(call, &["usage", "create", "tables"])?;
    let flag = |field: &str| match body.get(field) {
        Some(Value::Bool(value)) => Ok(*value),
        _ => Err(Problem::invalid(format!("{field} must be true or false."))),
    };
    let (usage, create) = (flag("usage")?, flag("create")?);
    let wrong = || {
        Problem::invalid(format!(
            "tables must be a list, without repeats, of: {}.",
            TABLE.join(", ")
        ))
    };
    let asked: Vec<&str> = body
        .get("tables")
        .and_then(Value::as_array)
        .ok_or_else(wrong)?
        .iter()
        .map(|p| p.as_str().filter(|p| TABLE.contains(p)).ok_or_else(wrong))
        .collect::<Result<_, _>>()?;
    if asked
        .iter()
        .collect::<std::collections::BTreeSet<_>>()
        .len()
        != asked.len()
    {
        return Err(wrong());
    }
    let write = || {
        let role = quoted(&name)?;
        let mut commands = guard(&name)?;
        commands.push(format!("REVOKE ALL ON SCHEMA {within} FROM {role};"));
        let held: Vec<&str> = [(usage, "USAGE"), (create, "CREATE")]
            .into_iter()
            .filter_map(|(held, word)| held.then_some(word))
            .collect();
        if !held.is_empty() {
            commands.push(format!(
                "GRANT {} ON SCHEMA {within} TO {role};",
                held.join(", ")
            ));
        }
        commands.push(format!(
            "REVOKE ALL ON ALL TABLES IN SCHEMA {within} FROM {role};"
        ));
        commands.push(format!(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {within} REVOKE ALL ON TABLES FROM {role};"
        ));
        if !asked.is_empty() {
            let list = asked.join(", ").to_uppercase();
            commands.push(format!(
                "GRANT {list} ON ALL TABLES IN SCHEMA {within} TO {role};"
            ));
            commands.push(format!(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} IN SCHEMA {within} GRANT {list} ON TABLES TO {role};"
            ));
        }
        Ok(block(&commands))
    };
    let after = privileges_sql(&name, Some(&schema))?;
    let rows = match change(store, host, call, at, "", write, Some(after))? {
        Ok(rows) => rows,
        Err(wait) => return Ok(wait),
    };
    let found = parsed(&rows, 0);
    let entry = found
        .as_ref()
        .and_then(|list| list.get(0))
        .ok_or_else(no_schema)?;
    Ok(Reply::ok(privileges_body(entry)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_access_level_is_a_set_of_memberships() {
        for access in ["none", "read", "read_write", "owner"] {
            assert_eq!(access_of(memberships(access).unwrap()), access);
        }
        assert_eq!(access_of(&[WRITE]), "custom");
        assert_eq!(access_of(&[READ, "pg_monitor"]), "custom");
        assert_eq!(access_of(&[OWNER, READ]), "owner");
        assert!(memberships("custom").is_err() && memberships("admin").is_err());
    }
    #[test]
    fn engine_roles_are_managed_and_have_no_access_level() {
        let role = |name: &str, superuser: bool| {
            role_body(
                &json!({"name": name, "superuser": superuser, "member_of": [],
                "connection_limit": -1}),
            )
        };
        for name in [
            "cloud_admin",
            "supabricks_owner",
            "pg_monitor",
            "neon_superuser",
            "sb_x",
        ] {
            let body = role(name, false);
            assert_eq!(
                (&body["managed"], &body["access"]),
                (&json!(true), &Value::Null),
                "{name}"
            );
            assert!(changeable(name).is_err(), "{name}");
        }
        assert_eq!(role("made_by_hand", true)["managed"], true);
        let plain = role("reporting", false);
        assert_eq!(
            (&plain["managed"], &plain["access"]),
            (&json!(false), &json!("none"))
        );
        assert_eq!(plain["connection_limit"], Value::Null);
        assert_eq!(changeable("reporting").unwrap(), "reporting");
        assert_eq!(changeable("odd%20name").unwrap(), "odd name");
    }
    #[test]
    fn a_block_cannot_be_closed_by_what_it_holds() {
        let hostile = "x\"; DROP ROLE cloud_admin; $$ END $sb$ --";
        let sql = block(&guard(hostile).unwrap());
        let tag = sql.split('$').nth(1).unwrap();
        assert_eq!(tag.len(), 34);
        assert_eq!(sql.matches(&format!("${tag}$")).count(), 2);
        assert!(sql.ends_with(&format!("END ${tag}$")));
        assert_eq!(quoted("a\"b").unwrap(), "\"a\"\"b\"");
        // A password never appears in a statement, only its digest.
        let stored = password_of("reporting", "plain-secret");
        assert!(stored.starts_with("'md5") && stored.len() == 37 && !stored.contains("plain"));
    }
    #[test]
    fn privileges_on_tables_are_what_every_table_carries() {
        let entry = |tables: u64, held: Value, future: Value| {
            privileges_body(&json!({"schema": "public", "usage": true, "create": false,
                "table_count": tables, "held": held, "future": future}))
        };
        let all = entry(
            3,
            json!({"SELECT": 3, "INSERT": 3}),
            json!(["SELECT", "INSERT"]),
        );
        assert_eq!(all["tables"], json!(["select", "insert"]));
        assert_eq!(all["future_tables"], json!(["select", "insert"]));
        assert_eq!(all["uniform"], true);
        let some = entry(3, json!({"SELECT": 3, "UPDATE": 1}), json!([]));
        assert_eq!(
            (&some["tables"], &some["uniform"]),
            (&json!(["select"]), &json!(false))
        );
        let empty = entry(0, json!({}), json!(["DELETE"]));
        assert_eq!(
            (&empty["tables"], &empty["future_tables"]),
            (&json!([]), &json!(["delete"]))
        );
        assert_eq!(empty["uniform"], true);
    }
    #[test]
    fn postgres_refusals_keep_their_meaning() {
        let error =
            |state: &str| json!({"code": "sql_error", "message": format!("{state}: detail")});
        for (state, status, code) in [
            ("42710", 409, "name_taken"),
            ("42704", 404, "not_found"),
            ("3F000", 404, "not_found"),
            ("SB001", 409, "conflict"),
            ("2BP01", 409, "conflict"),
            ("55P03", 503, "unavailable"),
            ("XX000", 500, "internal_error"),
        ] {
            let problem = refused(&error(state));
            assert_eq!((problem.status, problem.code), (status, code), "{state}");
        }
        assert_eq!(
            refused(&json!({"code": "unavailable", "message": "x"})).status,
            503
        );
    }
}
