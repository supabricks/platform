//! Emit the CRD YAML for the chart's crds/ directory:
//! `cargo run --bin crdgen > ../chart/crds/sspc-crds.yaml`

#[path = "../crd.rs"]
mod crd;

use kube::CustomResourceExt;

fn main() {
    let docs = [
        serde_yaml::to_string(&crd::Database::crd()).unwrap(),
        serde_yaml::to_string(&crd::Branch::crd()).unwrap(),
    ];
    print!("{}", docs.join("---\n"));
}
