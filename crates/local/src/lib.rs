//! Native configuration adapter. P01 does not start processes or open state.
use std::{net::SocketAddr, path::Path};
use supabricks_core::{
    error::{OperationError, ValidationError},
    resource::PgMajor,
    spec::{ComputePaths, Settings, SpecParams, render},
};

/// A reviewable plan; execution, authentication provisioning and ownership are
/// the daemon's responsibility in later slices. Commands must use argv directly.
pub struct ComputePlan {
    pub pg_major: PgMajor,
    pub config: serde_json::Value,
    pub command: Vec<String>,
}

pub struct ComputeInput<'a> {
    pub pg_major: PgMajor,
    pub bundle: &'a Path,
    pub data: &'a Path,
    pub config: &'a Path,
    pub compute_id: &'a str,
    pub sql: SocketAddr,
    pub external_http: SocketAddr,
    pub internal_http: SocketAddr,
}

pub fn plan_compute(
    input: &ComputeInput<'_>,
    spec: &SpecParams<'_>,
) -> Result<ComputePlan, OperationError> {
    let invalid = |message: &str| {
        ValidationError::new(
            message,
            "supply absolute paths, PG17, and distinct loopback ports",
        )
    };
    if input.pg_major != PgMajor::V17 {
        return Err(invalid("the native engine bundle supports PostgreSQL 17").into());
    }
    let addresses = [input.sql, input.external_http, input.internal_http];
    if addresses
        .iter()
        .any(|a| !a.ip().is_loopback() || a.port() == 0)
        || input.external_http.ip() != input.internal_http.ip()
        || input.sql.port() == input.external_http.port()
        || input.sql.port() == input.internal_http.port()
        || input.external_http.port() == input.internal_http.port()
    {
        return Err(invalid("invalid native compute listeners").into());
    }
    if input.compute_id.is_empty() || input.data == input.config {
        return Err(invalid("invalid compute identity or overlapping data/config paths").into());
    }
    let paths = ComputePaths {
        compute_ctl: input.bundle.join("bin/compute_ctl"),
        postgres: input.bundle.join(format!(
            "pg_install/v{}/bin/postgres",
            u16::from(input.pg_major)
        )),
        data: input.data.to_owned(),
        config: input.config.to_owned(),
    };
    let connstr = format!("postgresql://cloud_admin@{}/postgres", input.sql);
    let mut command = paths
        .command(input.compute_id, &connstr)
        .map_err(|e| invalid(&e.to_string()))?;
    command.extend([
        "--dev".into(),
        format!("--http-listen-addr={}", input.external_http.ip()),
        format!("--external-http-port={}", input.external_http.port()),
        format!("--internal-http-port={}", input.internal_http.port()),
    ]);
    let mut config = render(
        spec,
        &Settings {
            port: input.sql.port(),
            listen_addresses: &input.sql.ip().to_string(),
            fsync: true,
            unix_socket_directories: Some(""),
        },
    )
    .map_err(|e| match e.downcast::<ValidationError>() {
        Ok(error) => error,
        Err(error) => invalid(&error.to_string()),
    })?;
    // These optional fields describe the old P0 fixture, not a native operation.
    // P02 will supply durable operation identity when it owns execution.
    let body = config["spec"]
        .as_object_mut()
        .expect("rendered spec object");
    body.remove("timestamp");
    body.remove("operation_uuid");
    let cluster = body["cluster"]
        .as_object_mut()
        .expect("rendered cluster object");
    for field in ["cluster_id", "name", "state"] {
        cluster.remove(field);
    }
    Ok(ComputePlan {
        pg_major: input.pg_major,
        config,
        command,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn params() -> SpecParams<'static> {
        SpecParams {
            tenant_id: "00000000000000000000000000000001",
            timeline_id: "00000000000000000000000000000002",
            encrypted_password: "deadbeef",
            jwks_x_b64url: "x",
            jwks_kid_b64url: "k",
            safekeepers: "127.0.0.1:5454",
            pageserver_connstring: "host=127.0.0.1 port=6400 application_name='X_B64URL \\\"test\\\"'",
        }
    }
    fn input() -> ComputeInput<'static> {
        ComputeInput {
            pg_major: PgMajor::V17,
            bundle: Path::new("/tmp/bundle with spaces"),
            data: Path::new("/tmp/cell/compute"),
            config: Path::new("/tmp/cell/spec.json"),
            compute_id: "main",
            sql: "127.0.0.1:55433".parse().unwrap(),
            external_http: "127.0.0.1:3080".parse().unwrap(),
            internal_http: "127.0.0.1:3081".parse().unwrap(),
        }
    }
    #[test]
    fn native_pg17_plan_preserves_paths_and_structured_values() {
        let p = params();
        let plan = plan_compute(&input(), &p).unwrap();
        assert_eq!(plan.pg_major, PgMajor::V17);
        assert!(plan.config["spec"].get("operation_uuid").is_none());
        assert!(plan.config["spec"].get("timestamp").is_none());
        assert!(plan.config["spec"]["cluster"].get("cluster_id").is_none());
        assert!(plan.command.contains(&"--dev".into()));
        assert_eq!(plan.command[0], "/tmp/bundle with spaces/bin/compute_ctl");
        assert!(
            plan.command
                .contains(&"--pgbin=/tmp/bundle with spaces/pg_install/v17/bin/postgres".into())
        );
        assert!(
            plan.command
                .contains(&"--http-listen-addr=127.0.0.1".into())
        );
        let settings = plan.config["spec"]["cluster"]["settings"]
            .as_array()
            .unwrap();
        for (name, value) in [
            ("port", "55433"),
            ("listen_addresses", "127.0.0.1"),
            ("fsync", "on"),
            ("unix_socket_directories", ""),
            ("neon.pageserver_connstring", p.pageserver_connstring),
        ] {
            assert_eq!(
                settings.iter().find(|s| s["name"] == name).unwrap()["value"],
                value
            );
        }
    }
    #[test]
    fn rejects_invalid_native_configuration() {
        let p = params();
        let mut i = input();
        i.pg_major = PgMajor::V16;
        assert!(plan_compute(&i, &p).is_err());
        i = input();
        i.sql = "0.0.0.0:5432".parse().unwrap();
        assert!(plan_compute(&i, &p).is_err());
        i = input();
        i.internal_http = i.external_http;
        assert!(plan_compute(&i, &p).is_err());
        i = input();
        i.bundle = Path::new("relative");
        assert!(plan_compute(&i, &p).is_err());
        let mut p = params();
        p.timeline_id = "bad";
        assert!(plan_compute(&input(), &p).is_err());
    }
}
