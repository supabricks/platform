//! The chart's security contract, enforced against rendered manifests.
//! Runs as part of `cargo test` (helm is a required repo tool), so the gate
//! needs no Python or extra packages — chart/check-hardening.sh is a thin
//! wrapper around this test. Named exceptions live in chart/hardening.md;
//! weakening the baseline without updating both places fails here.

use serde::Deserialize;
use serde_yaml::Value;
use std::collections::BTreeSet;
use std::process::Command;

fn rendered_docs() -> Vec<Value> {
    let chart = concat!(env!("CARGO_MANIFEST_DIR"), "/../../chart");
    let out = Command::new("helm")
        .args(["template", "sspc", chart, "-n", "sspc-cell"])
        .output()
        .expect("helm not found — it is a required tool (see handbook dev-loop)");
    assert!(
        out.status.success(),
        "helm template failed:\n{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let text = String::from_utf8(out.stdout).unwrap();
    serde_yaml::Deserializer::from_str(&text)
        .filter_map(|d| Value::deserialize(d).ok())
        .filter(|v: &Value| !v.is_null())
        .collect()
}

fn s<'a>(v: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut cur = v;
    for p in path {
        cur = cur.get(p)?;
    }
    Some(cur)
}

fn str_at<'a>(v: &'a Value, path: &[&str]) -> Option<&'a str> {
    s(v, path)?.as_str()
}

#[test]
fn chart_hardening_contract() {
    let docs = rendered_docs();
    let mut fails: Vec<String> = Vec::new();

    // Named exception: the operator is the single API-token consumer.
    let token_allowed: BTreeSet<&str> = ["sspc-operator"].into();

    let mut pods = Vec::new(); // (workload name, pod template)
    for d in &docs {
        let kind = str_at(d, &["kind"]).unwrap_or("");
        if matches!(kind, "Deployment" | "StatefulSet" | "Job") {
            let name = str_at(d, &["metadata", "name"]).unwrap_or("?").to_string();
            if let Some(tpl) = s(d, &["spec", "template"]) {
                pods.push((name, tpl.clone()));
            }
        }
    }
    assert!(
        pods.len() >= 9,
        "expected >=9 workloads, got {}",
        pods.len()
    );

    for (name, tpl) in &pods {
        let spec = s(tpl, &["spec"]).cloned().unwrap_or(Value::Null);
        // Token automount is explicit everywhere; only the operator mounts one.
        let am = s(&spec, &["automountServiceAccountToken"]).and_then(|v| v.as_bool());
        if token_allowed.contains(name.as_str()) {
            if am != Some(true) {
                fails.push(format!(
                    "{name}: operator must set automountServiceAccountToken: true explicitly"
                ));
            }
        } else if am != Some(false) {
            fails.push(format!(
                "{name}: automountServiceAccountToken must be false"
            ));
        }
        // Pod seccomp everywhere.
        if str_at(&spec, &["securityContext", "seccompProfile", "type"]) != Some("RuntimeDefault") {
            fails.push(format!("{name}: pod seccompProfile RuntimeDefault missing"));
        }
        // Every container drops privileges; no credential-looking literal envs.
        let mut containers: Vec<Value> = Vec::new();
        for key in ["containers", "initContainers"] {
            if let Some(list) = s(&spec, &[key]).and_then(|v| v.as_sequence()) {
                containers.extend(list.iter().cloned());
            }
        }
        for c in &containers {
            let cname = str_at(c, &["name"]).unwrap_or("?");
            if s(c, &["securityContext", "allowPrivilegeEscalation"]).and_then(|v| v.as_bool())
                != Some(false)
            {
                fails.push(format!(
                    "{name}/{cname}: allowPrivilegeEscalation false missing"
                ));
            }
            let drops_all = s(c, &["securityContext", "capabilities", "drop"])
                .and_then(|v| v.as_sequence())
                .is_some_and(|d| d.iter().any(|x| x.as_str() == Some("ALL")));
            if !drops_all {
                fails.push(format!("{name}/{cname}: capabilities drop ALL missing"));
            }
            if let Some(envs) = s(c, &["env"]).and_then(|v| v.as_sequence()) {
                for e in envs {
                    let ename = str_at(e, &["name"]).unwrap_or("").to_uppercase();
                    let literal = s(e, &["value"]).is_some();
                    let cred = ["PASSWORD", "SECRET", "ACCESS_KEY", "TOKEN"]
                        .iter()
                        .any(|t| ename.contains(t));
                    if literal && cred {
                        fails.push(format!(
                            "{name}/{cname}: credential env {ename} is a literal value — use secretKeyRef"
                        ));
                    }
                }
            }
        }
        // Expected-digest annotation on every pod.
        if str_at(tpl, &["metadata", "annotations", "sspc.io/image-digest"]).is_none() {
            fails.push(format!("{name}: sspc.io/image-digest annotation missing"));
        }
    }

    // The network boundary renders by default.
    let policies: Vec<&str> = docs
        .iter()
        .filter(|d| str_at(d, &["kind"]) == Some("NetworkPolicy"))
        .filter_map(|d| str_at(d, &["metadata", "name"]))
        .collect();
    if !policies.contains(&"default-deny-ingress") {
        fails.push("NetworkPolicy default-deny-ingress not rendered".into());
    }
    if policies.len() < 10 {
        fails.push(format!(
            "expected >=10 NetworkPolicies, got {}: {policies:?}",
            policies.len()
        ));
    }

    // Operator RBAC must not grow without review (verb-set pin). `update` is
    // required by kube-rs finalizer machinery on CR status/finalizers.
    let allowed_verbs: BTreeSet<&str> = [
        "get", "list", "watch", "create", "patch", "update", "delete",
    ]
    .into();
    for d in &docs {
        if str_at(d, &["kind"]) == Some("Role")
            && str_at(d, &["metadata", "name"]) == Some("sspc-operator")
        {
            for rule in s(d, &["rules"])
                .and_then(|v| v.as_sequence())
                .into_iter()
                .flatten()
            {
                let verbs: BTreeSet<&str> = s(rule, &["verbs"])
                    .and_then(|v| v.as_sequence())
                    .into_iter()
                    .flatten()
                    .filter_map(|v| v.as_str())
                    .collect();
                if !verbs.is_subset(&allowed_verbs) {
                    fails.push(format!(
                        "RBAC verbs grew beyond the pinned set: {verbs:?} — review required"
                    ));
                }
            }
        }
    }

    assert!(
        fails.is_empty(),
        "HARDENING CONTRACT VIOLATIONS:\n - {}",
        fails.join("\n - ")
    );
    println!(
        "hardening contract OK: {} workloads, {} network policies",
        pods.len(),
        policies.len()
    );
}
