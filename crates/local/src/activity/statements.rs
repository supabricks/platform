//! Cumulative local application statement counters, never guessed percentiles.
use super::sql::{bound_text, pg};
use super::*;
use tokio_postgres::Client;
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Order {
    #[default]
    TotalTime,
    MeanTime,
    Calls,
    Rows,
}
impl Order {
    fn column(self) -> &'static str {
        match self {
            Self::TotalTime => "total_exec_time",
            Self::MeanTime => "mean_exec_time",
            Self::Calls => "calls",
            Self::Rows => "rows",
        }
    }
}
pub(super) struct Request {
    pub include_query_text: bool,
    pub limit: u16,
    pub order_by: Order,
}
pub(super) fn validate_limit(limit: u16) -> Result<()> {
    if !(1..=100).contains(&limit) {
        return Err(invalid("statement limit must be between 1 and 100"));
    }
    Ok(())
}
fn unavailable(reason: &str) -> Value {
    json!({"available":false,"reason":reason,"statements":[],"p95_available":false,"p95_unavailable_reason":"latency_histogram_not_collected","query_text_persisted":false})
}
fn share(total: f64, all: f64) -> Option<f64> {
    if !total.is_finite() || !all.is_finite() || total < 0.0 || all <= 0.0 {
        None
    } else {
        Some((total / all * 100.0).clamp(0.0, 100.0))
    }
}
async fn epoch(client: &Client) -> Result<(String, i64)> {
    let row = client
        .query_one(
            "SELECT stats_reset::text,dealloc FROM supabricks_extensions.pg_stat_statements_info",
            &[],
        )
        .await
        .map_err(pg)?;
    Ok((row.get(0), row.get(1)))
}
pub(super) async fn read(client: &Client, request: Request) -> Result<Value> {
    validate_limit(request.limit)?;
    // Only the protected extension/schema installed by #253 is used. Never
    // discover or execute similarly named user functions via search_path.
    let extension=client.query_opt(r#"SELECT e.oid::text,e.extversion,
        e.extowner='cloud_admin'::regrole AND n.nspname='supabricks_extensions' AND n.nspowner='cloud_admin'::regrole
        AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a WHERE a.privilege_type='CREATE' AND a.grantee<>n.nspowner)
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_stat_statements'"#,&[]).await.map_err(pg)?;
    let Some(extension) = extension else {
        return Ok(unavailable("extension_not_enabled"));
    };
    if !extension.get::<_, bool>(2) {
        return Ok(unavailable("extension_outside_managed_schema"));
    }
    let version: String = extension.get(1);
    if version != "1.11" {
        return Ok(unavailable("unsupported_extension_version"));
    }
    let settings=client.query_one(r#"SELECT current_setting('shared_preload_libraries'),current_setting('compute_query_id'),
        current_setting('pg_stat_statements.track',true),current_setting('pg_stat_statements.track_utility',true),
        current_setting('pg_stat_statements.max',true),pg_postmaster_start_time()::text"#,&[]).await.map_err(pg)?;
    let preload: String = settings.get(0);
    if !preload.split(',').any(|v| v.trim() == "pg_stat_statements") {
        return Ok(unavailable("compute_restart_required"));
    }
    let max_entries: Option<String> = settings.get(4);
    let Some(max_entries) = max_entries
        .and_then(|n| n.parse::<u32>().ok())
        .filter(|n| *n <= 10_000)
    else {
        return Ok(unavailable("statement_capacity_exceeds_10000"));
    };
    let before = epoch(client).await?;
    // Window aggregates run before LIMIT so shares include every eligible
    // retained statement, not merely the displayed top-N. Internal roles,
    // other databases and nested execution are excluded from both sides.
    let sql = format!(
        r#"WITH eligible AS MATERIALIZED (
        SELECT s.userid,s.dbid,s.queryid,s.calls,s.total_exec_time,s.mean_exec_time,s.rows,
          CASE WHEN $1 THEN left(s.query,1024) END AS query,s.stats_since
        FROM supabricks_extensions.pg_stat_statements($1) s
        JOIN pg_roles r ON r.oid=s.userid
        WHERE s.dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
          AND r.rolname='supabricks_owner' AND NOT r.rolsuper AND s.toplevel AND s.calls>0
      )
      SELECT userid::text,dbid::text,queryid::text,calls,total_exec_time,mean_exec_time,rows,
        CASE WHEN $1 THEN left(query,1024) END,stats_since::text,
        sum(total_exec_time) OVER(),count(*) OVER()
      FROM eligible ORDER BY {} DESC,queryid,userid,dbid LIMIT $2"#,
        request.order_by.column()
    );
    let limit = i64::from(request.limit);
    let rows = client
        .query(&sql, &[&request.include_query_text, &limit])
        .await
        .map_err(pg)?;
    let after = epoch(client).await?;
    if before != after {
        return Ok(unavailable("statistics_changed_during_read"));
    }
    let total = rows.first().map(|r| r.get::<_, f64>(9)).unwrap_or(0.0);
    let count = rows.first().map(|r| r.get::<_, i64>(10)).unwrap_or(0);
    let statements:Vec<_>=rows.into_iter().map(|r| {
        let text:Option<String>=r.get(7);
        let elapsed:f64=r.get(4);
        json!({"user_oid":r.get::<_,String>(0),"database_oid":r.get::<_,String>(1),"query_id":r.get::<_,String>(2),"toplevel":true,
          "calls":r.get::<_,i64>(3),"total_exec_time_ms":elapsed,"mean_exec_time_ms":r.get::<_,f64>(5),"rows":r.get::<_,i64>(6),
          "query_text":text.map(bound_text),"query_text_policy":if request.include_query_text {"postgres_representative_bounded_not_guaranteed_redacted"}else{"hidden"},
          "stats_since":r.get::<_,String>(8),"share_of_total_exec_time_pct":share(elapsed,total),"p95_exec_time_ms":null})
    }).collect();
    let track: Option<String> = settings.get(2);
    let query_id: String = settings.get(1);
    let result = json!({"available":true,"scope":{"database":"postgres","role":"supabricks_owner","toplevel_only":true},"window":"cumulative_per_entry",
        "statements":statements,"order_by":request.order_by,"limit":request.limit,"eligible_statement_count":count,"truncated":count>i64::from(request.limit),
        "total_exec_time_ms":total,"observed_at_ms":crate::identity::now(),"consistent_snapshot":false,
        "epoch":{"extension_oid":extension.get::<_,String>(0),"extension_version":version,"postmaster_start":settings.get::<_,String>(5),"stats_reset":before.0},
        "deallocations":before.1,"collection":{"track":track,"track_utility":settings.get::<_,Option<String>>(3),"compute_query_id":query_id,"max_entries":max_entries,"tracking_enabled":track.as_deref().is_some_and(|v|v!="none") && query_id!="off"},
        "p95_available":false,"p95_unavailable_reason":"latency_histogram_not_collected","query_text_persisted":false,"governed_supported":false});
    if serde_json::to_vec(&result)?.len() > 512 * 1024 {
        return Err(conflict("statement response exceeds 512 KiB"));
    }
    Ok(result)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn limits_and_sort_keys_cannot_become_arbitrary_sql() {
        assert!(validate_limit(0).is_err());
        assert!(validate_limit(101).is_err());
        assert!(validate_limit(100).is_ok());
        assert!(serde_json::from_value::<Order>(json!("calls;DROP TABLE data")).is_err());
        assert_eq!(Order::MeanTime.column(), "mean_exec_time");
    }
    #[test]
    fn execution_share_is_defined_only_for_positive_observed_totals() {
        assert_eq!(share(2.0, 10.0), Some(20.0));
        assert_eq!(share(0.0, 0.0), None);
        assert_eq!(share(f64::NAN, 10.0), None);
        assert_eq!(share(-1.0, 10.0), None);
    }
}
