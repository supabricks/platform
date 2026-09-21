//! Explicit browser projection: no local storage URIs, endpoint or service diagnostics.
use serde_json::{Value, json};
pub(crate) fn public_view(mut value: Value) -> Value {
    if let Some(health) = value.get_mut("catalog") {
        *health = json!({"mode":health["mode"],"state":health["state"],"ready":health["ready"]});
    }
    let item = if value.get("publication").is_some() {
        &mut value["publication"]
    } else {
        &mut value
    };
    if let Some(tables) = item.get_mut("tables").and_then(Value::as_array_mut) {
        for t in tables {
            if let Some(body) = t.get_mut("body").and_then(Value::as_object_mut) {
                body.remove("storage_location");
            }
        }
    }
    // Publication diagnostics are free-form. Metadata faults already use the
    // public typed contract with static, credential-free messages.
    if let Some(error) = item.get_mut("error").filter(|v| v.is_string()) {
        *error = json!(
            "Publication needs attention. Inspect catalog diagnostics and resume explicitly."
        );
    }
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn projections_preserve_review_identity_without_private_paths() {
        let v = public_view(
            json!({"preview_hash":"review","tables":[{"body":{"name":"orders","storage_location":"file:///private/root"}}],"catalog":{"endpoint":"http://private","ready":true,"mode":"local"}}),
        );
        assert_eq!(v["preview_hash"], "review");
        assert!(!v.to_string().contains("private"));
        let v = public_view(
            json!({"publication":{"id":"op","error":"/private/file","tables":[{"body":{"storage_location":"file:///private"}}]}}),
        );
        assert_eq!(v["publication"]["id"], "op");
        assert!(!v.to_string().contains("private"));
        let fault = json!({"state":"failed","error":{"code":"schema_drift","retryable":false,"message":"schema changed; inspect and explicitly rebind"}});
        assert_eq!(public_view(fault.clone()), fault);
    }
}
