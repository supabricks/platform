# sspc platform tasks. Two aggregate gates mirror CI exactly:
#   just verify-static   == the CI "unit" job (no cluster needed)
#   just verify-runtime  == the CI "e2e" job's test stages (needs the platform up)

# ---- static gates (CI parity: unit job) ----

fmt-check:
    cargo fmt -- --check

test:
    cargo test --locked

crd-check:
    cargo run --locked --bin crdgen | diff -u chart/crds/sspc-crds.yaml -

helm-lint:
    helm lint chart

hardening:
    ./chart/check-hardening.sh

ui-test:
    cd ui && npm test

ui-build:
    cd ui && npm run build

verify-static: fmt-check test crd-check helm-lint ui-test ui-build

# ---- local component baseline (separate native-baseline CI workflow) ----
# Activate components/.venv first; see components/README.md.

component-check:
    python3 components/validate.py

component-test:
    python3 -m unittest discover -s components -p 'test_*.py' -v

verify-native-baseline: component-check component-test

# ---- runtime gates (CI parity: e2e job) ----

e2e:
    ./e2e/run.sh

chaos:
    ./e2e/chaos.sh

restore:
    ./e2e/restore.sh

verify-runtime: e2e chaos restore

# ---- dev loop ----

build:
    cargo build

crdgen:
    cargo run --bin crdgen > chart/crds/sspc-crds.yaml

image:
    docker build -t sspc-operator:p1 .

# Build + load + restart: the ONLY safe repeat-deploy path on kind (see
# handbook dev-loop; the installer is for install, not iterating).
deploy: image
    docker save --platform linux/arm64 -o /tmp/sspc-op.tar sspc-operator:p1 || docker save -o /tmp/sspc-op.tar sspc-operator:p1
    kind load image-archive --name sspc /tmp/sspc-op.tar
    rm -f /tmp/sspc-op.tar
    kubectl apply -f chart/crds/
    kubectl -n sspc-cell rollout restart deploy/sspc-operator
    kubectl -n sspc-cell rollout status deploy/sspc-operator --timeout=120s

up:
    ./install/up.sh

down:
    ./install/down.sh
