//! Interval quantile bounds from the shipped Neon's fixed executor histogram.
//! No query text, per-query identifiers, interpolation, or percentile averaging.
use super::*;
use tokio_postgres::Client;
// The C collector places durations strictly below these microsecond bounds.
// Its SQL column is named bucket_le, but equality goes into the next bucket.
const UPPER_US: [u64; 32] = [
    2, 3, 6, 10, 20, 30, 60, 100, 200, 300, 600, 1000, 2000, 3000, 6000, 10000, 20000, 30000,
    60000, 100000, 200000, 300000, 600000, 1000000, 2000000, 3000000, 6000000, 10000000, 20000000,
    30000000, 60000000, 100000000,
];
const EXACT_FLOAT_INTEGER: u64 = 1 << 53;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Snapshot {
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    cumulative: Option<Vec<u64>>,
}
fn unavailable(reason: &str) -> Snapshot {
    Snapshot {
        status: reason.into(),
        cumulative: None,
    }
}
fn decode(rows: Vec<(String, Option<f64>, f64)>) -> Snapshot {
    if rows.len() != 34 {
        return unavailable("histogram_layout_unsupported");
    }
    let mut buckets = vec![None; 33];
    let mut total = None;
    for (name, bound, count) in rows {
        if !count.is_finite()
            || count < 0.
            || count.fract() != 0.
            || count >= EXACT_FLOAT_INTEGER as f64
        {
            return unavailable("histogram_counter_not_exact");
        }
        if name == "query_time_seconds_count" && bound.is_none() {
            if total.replace(count as u64).is_some() {
                return unavailable("histogram_layout_unsupported");
            }
        } else if name == "query_time_seconds_bucket" {
            let index = match bound {
                Some(f) if f == f64::INFINITY => Some(32),
                Some(f) => UPPER_US.iter().position(|n| *n as f64 / 1_000_000. == f),
                None => None,
            };
            let Some(index) = index else {
                return unavailable("histogram_layout_unsupported");
            };
            if buckets[index].replace(count as u64).is_some() {
                return unavailable("histogram_layout_unsupported");
            }
        } else {
            return unavailable("histogram_layout_unsupported");
        }
    }
    let Some(buckets) = buckets.into_iter().collect::<Option<Vec<_>>>() else {
        return unavailable("histogram_layout_unsupported");
    };
    if buckets.windows(2).any(|p| p[0] > p[1]) || buckets.last().copied() != total {
        return unavailable("histogram_read_inconsistent");
    }
    Snapshot {
        status: "available".into(),
        cumulative: Some(buckets),
    }
}
pub(super) async fn read(client: &Client) -> Snapshot {
    match read_inner(client).await {
        Ok(snapshot) => snapshot,
        Err(_) => unavailable("histogram_probe_failed"),
    }
}
async fn read_inner(client: &Client) -> std::result::Result<Snapshot, tokio_postgres::Error> {
    // SET (rather than a connection option) preserves the inherited reset_val.
    // The sampler disables its own instrumentation before any executor query.
    let enabled=client.query_opt("SELECT reset_val='on' FROM pg_settings WHERE name='neon.monitor_query_exec_time' AND vartype='bool'",&[]).await?;
    let Some(enabled) = enabled else {
        return Ok(unavailable("histogram_not_supported"));
    };
    if !enabled.get::<_, bool>(0) {
        return Ok(unavailable("histogram_disabled_for_default_session"));
    }
    // Call only the known release-local C implementation, never a replaceable
    // view or an application-defined function. The provider has no DDL effects.
    let provider=client.query_opt(r#"SELECT e.extversion IN ('1.5','1.6') AND e.extowner='cloud_admin'::regrole
        AND n.nspname='neon' AND n.nspowner='cloud_admin'::regrole
        AND p.proowner='cloud_admin'::regrole AND l.lanname='c'
        AND p.probin='$libdir/neon' AND p.prosrc='neon_get_perf_counters'
        AND NOT EXISTS(SELECT 1 FROM aclexplode(coalesce(n.nspacl,acldefault('n',n.nspowner))) a WHERE a.privilege_type='CREATE' AND a.grantee<>n.nspowner)
        FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
        JOIN pg_proc p ON p.pronamespace=n.oid AND p.proname='get_perf_counters' AND p.pronargs=0
        JOIN pg_language l ON l.oid=p.prolang
        JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.refclassid='pg_extension'::regclass AND d.refobjid=e.oid AND d.deptype='e'
        WHERE e.extname='neon'"#,&[]).await?;
    if !provider.is_some_and(|r| r.get::<_, bool>(0)) {
        return Ok(unavailable("histogram_provider_untrusted_or_unsupported"));
    }
    let rows=client.query(r#"SELECT metric,bucket_le,value FROM neon.get_perf_counters() AS p(metric text,bucket_le float8,value float8)
        WHERE metric IN ('query_time_seconds_count','query_time_seconds_bucket') LIMIT 35"#,&[]).await?;
    Ok(decode(
        rows.into_iter()
            .map(|r| (r.get(0), r.get(1), r.get(2)))
            .collect(),
    ))
}
pub(super) fn empty(status: &str) -> Value {
    json!({"status":status,"sample_count":null,"interval_ms":null,"p50_ms":null,"p95_ms":null,"p99_ms":null})
}
fn bounds(counts: &[u64], total: u64, percent: u64) -> Value {
    let rank = ((total as u128) * (percent as u128)).div_ceil(100) as u64;
    let mut count = 0;
    for (i, n) in counts.iter().enumerate() {
        count += n;
        if count >= rank {
            let lower = if i == 0 {
                0.
            } else {
                UPPER_US[i - 1] as f64 / 1000.
            };
            let upper = UPPER_US.get(i).map(|n| *n as f64 / 1000.);
            return json!([lower, upper]);
        }
    }
    Value::Null
}
pub(super) fn point(sample: &Sample, previous: Option<&Sample>) -> Value {
    let Some(raw) = &sample.raw else {
        return empty(&sample.status);
    };
    let Some(snapshot) = &raw.latency else {
        return empty("not_collected");
    };
    let Some(now) = &snapshot.cumulative else {
        return empty(&snapshot.status);
    };
    let Some(prev) = previous else {
        return empty("no_baseline");
    };
    let Some(old_raw) = &prev.raw else {
        return empty("gap_or_restart");
    };
    let Some(old) = old_raw.latency.as_ref().and_then(|s| s.cumulative.as_ref()) else {
        return empty("no_baseline");
    };
    let elapsed = sample.at_ms - prev.at_ms;
    if elapsed <= 0
        || elapsed > INTERVAL_MS * 3
        || sample.generation != prev.generation
        || sample.revision != prev.revision
        || raw.postmaster_start != old_raw.postmaster_start
    {
        return empty("gap_or_restart");
    }
    if now.len() != 33 || old.len() != 33 {
        return empty("histogram_layout_unsupported");
    }
    let mut deltas = Vec::with_capacity(33);
    let mut previous_now = 0;
    let mut previous_old = 0;
    for (&n, &o) in now.iter().zip(old) {
        let Some(n_bucket) = n.checked_sub(previous_now) else {
            return empty("histogram_read_inconsistent");
        };
        let Some(o_bucket) = o.checked_sub(previous_old) else {
            return empty("histogram_read_inconsistent");
        };
        let Some(delta) = n_bucket.checked_sub(o_bucket) else {
            return empty("histogram_counter_regression");
        };
        deltas.push(delta);
        previous_now = n;
        previous_old = o;
    }
    let total = deltas.iter().sum::<u64>();
    let mut value = empty(if total == 0 {
        "no_observations"
    } else {
        "available"
    });
    value["sample_count"] = json!(total);
    value["interval_ms"] = json!(elapsed);
    if total > 0 {
        for (name, p) in [("p50_ms", 50), ("p95_ms", 95), ("p99_ms", 99)] {
            value[name] = bounds(&deltas, total, p);
        }
    }
    value
}
#[cfg(test)]
mod tests {
    use super::*;
    fn rows() -> Vec<(String, Option<f64>, f64)> {
        let mut rows = vec![("query_time_seconds_count".into(), None, 10.)];
        rows.extend(UPPER_US.iter().map(|b| {
            (
                "query_time_seconds_bucket".into(),
                Some(*b as f64 / 1_000_000.),
                10.,
            )
        }));
        rows.push(("query_time_seconds_bucket".into(), Some(f64::INFINITY), 10.));
        rows
    }
    #[test]
    fn source_contract_rejects_incoherent_and_imprecise_scrapes() {
        assert_eq!(decode(rows()).status, "available");
        let mut bad = rows();
        bad[0].2 = 11.;
        assert_eq!(decode(bad).status, "histogram_read_inconsistent");
        let mut bad = rows();
        bad[2].2 = 9.;
        assert_eq!(decode(bad).status, "histogram_read_inconsistent");
        let mut bad = rows();
        bad[4].1 = Some(0.007);
        assert_eq!(decode(bad).status, "histogram_layout_unsupported");
        let mut bad = rows();
        bad[1].2 = EXACT_FLOAT_INTEGER as f64;
        assert_eq!(decode(bad).status, "histogram_counter_not_exact");
    }
    #[test]
    fn interval_histograms_fence_resets_and_do_not_invent_quiet_percentiles() {
        let mut a = super::super::tests::sample(30_000);
        a.raw.as_mut().unwrap().latency = Some(Snapshot {
            status: "available".into(),
            cumulative: Some(vec![0; 33]),
        });
        let mut b = a.clone();
        b.at_ms = 60_000;
        let mut buckets = vec![50; 33];
        buckets[11..32].fill(95);
        buckets[32] = 100;
        b.raw.as_mut().unwrap().latency.as_mut().unwrap().cumulative = Some(buckets);
        let result = point(&b, Some(&a));
        assert_eq!(result["sample_count"], 100);
        assert_eq!(result["p95_ms"], json!([0.6, 1.]));
        let mut quiet = b.clone();
        quiet.at_ms += 30_000;
        assert_eq!(point(&quiet, Some(&b))["status"], "no_observations");
        assert!(point(&quiet, Some(&b))["p50_ms"].is_null());
        let mut reset = a.clone();
        reset.at_ms = 90_000;
        assert_eq!(
            point(&reset, Some(&b))["status"],
            "histogram_counter_regression"
        );
        for change in 0..4 {
            let mut b = b.clone();
            match change {
                0 => b.generation += 1,
                1 => b.revision += 1,
                2 => b.at_ms = 180_000,
                _ => b.raw.as_mut().unwrap().postmaster_start = "new".into(),
            };
            assert_eq!(point(&b, Some(&a))["status"], "gap_or_restart");
        }
        let mut encoded = serde_json::to_value(&a).unwrap();
        encoded["raw"].as_object_mut().unwrap().remove("latency");
        let old: Sample = serde_json::from_value(encoded).unwrap();
        assert_eq!(point(&old, None)["status"], "not_collected");
        let cumulative = vec![EXACT_FLOAT_INTEGER - 1; 33];
        b.raw.as_mut().unwrap().latency.as_mut().unwrap().cumulative = Some(cumulative);
        let mut large = serde_json::to_value(&b).unwrap();
        for name in [
            "commits",
            "rollbacks",
            "hits",
            "reads",
            "wal_bytes",
            "database_size_bytes",
        ] {
            large["raw"][name] = json!(u64::MAX);
        }
        for name in ["postmaster_start", "database_reset", "wal_reset"] {
            large["raw"][name] = json!("2026-10-12 01:23:45.123456+00");
        }
        large["at_ms"] = json!(1_800_000_000_000i64);
        large["compute"] = json!({"status":"available","at_ms":1_800_000_000_000i64,
            "raw":{"root_identity":"18446744073709551615","elapsed_ns":u64::MAX,
            "cpu_ns":u64::MAX,"rss_sum_bytes":u64::MAX,"process_count":512,"collection_ns":u64::MAX}});
        let large: Sample = serde_json::from_value(large).unwrap();
        assert!(serde_json::to_vec(&large).unwrap().len() < 2048);
    }
    #[test]
    fn quantiles_use_nearest_rank_bucket_bounds_including_open_tail() {
        let mut counts = vec![0; 33];
        counts[0] = 50;
        counts[11] = 45;
        counts[32] = 5;
        assert_eq!(bounds(&counts, 100, 50), json!([0., 0.002]));
        assert_eq!(bounds(&counts, 100, 95), json!([0.6, 1.]));
        assert_eq!(bounds(&counts, 100, 99), json!([100000., null]));
    }
}
