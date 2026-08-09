//! Compute-spec rendering. The template is the spike's known-good spec
//! (spike/k8s/compute/spec-template.json) with the same placeholder scheme,
//! so rendering is golden-tested byte-for-byte against the P0 fixture.

use serde_json::Value;

const TEMPLATE: &str = include_str!("../assets/compute-spec-template.json");

pub struct SpecParams<'a> {
    pub tenant_id: &'a str,
    pub timeline_id: &'a str,
    pub jwks_x_b64url: &'a str,
    pub jwks_kid_b64url: &'a str,
}

/// Render the compute spec for compute_ctl's `--config`.
/// String-replace then parse: identical semantics to the P0 sed pipeline.
pub fn render(p: &SpecParams) -> anyhow::Result<Value> {
    let s = TEMPLATE
        .replace("TENANT_ID", p.tenant_id)
        .replace("TIMELINE_ID", p.timeline_id)
        .replace("KID_B64URL", p.jwks_kid_b64url)
        .replace("X_B64URL", p.jwks_x_b64url);
    Ok(serde_json::from_str(&s)?)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The P0 golden: rendered spec == the exact spec a live compute served
    /// data with on kind (tenant dbd27…, spike/NOTES.md Day 3).
    #[test]
    fn golden_matches_p0_spec() {
        let golden: Value =
            serde_json::from_str(include_str!("../tests/fixtures/known-good-spec.json")).unwrap();
        let rendered = render(&SpecParams {
            tenant_id: "dbd271b86f9fa29a8842ac23f67fede5",
            timeline_id: "461d39b24a3592dc712a379c0d3ab6e5",
            jwks_x_b64url: "mOLh_FkiKWIGG-AX-yKDcD1KiNvxkk_dcePrTF1GX0c",
            jwks_kid_b64url: "mxha6Szurut0u5Hz0JT058YlUi8tryLtYBBBiUokYtA",
        })
        .unwrap();
        assert_eq!(rendered, golden);
    }

    #[test]
    fn tenant_and_timeline_land_in_gucs() {
        let v = render(&SpecParams {
            tenant_id: "t1",
            timeline_id: "l1",
            jwks_x_b64url: "x",
            jwks_kid_b64url: "k",
        })
        .unwrap();
        let settings = v["spec"]["cluster"]["settings"].as_array().unwrap();
        let get = |name: &str| {
            settings
                .iter()
                .find(|s| s["name"] == name)
                .map(|s| s["value"].as_str().unwrap().to_string())
        };
        assert_eq!(get("neon.tenant_id").unwrap(), "t1");
        assert_eq!(get("neon.timeline_id").unwrap(), "l1");
        assert_eq!(v["compute_ctl_config"]["jwks"]["keys"][0]["x"], "x");
    }
}
