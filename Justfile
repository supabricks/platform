# sspc platform tasks
test:
    cargo test

build:
    cargo build

crdgen:
    cargo run --bin crdgen > chart/crds/sspc-crds.yaml

image:
    docker build -t sspc-operator:p1 .

up:
    ./install/up.sh

down:
    ./install/down.sh

e2e:
    ./e2e/run.sh
