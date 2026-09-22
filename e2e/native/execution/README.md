# UC09.4 isolated execution qualification

Read the [architecture, supported bounds and operator setup](../../../docs/architecture/uc094-isolated-execution.md).
This is Linux x86_64 source qualification. Shared product ingress stays disabled.
Docker access is required; only the trusted launcher container is privileged.
The real notebook code runs inside gVisor without host sockets or credentials.

1. Prepare the exact runtime/gVisor inputs using [IAM00](../iam/README.md).
2. Run `prepare.py --inputs '/absolute/uc094 inputs' --output /absolute/private-config.json`.
3. Build the pinned UC server using `components/build-unity-catalog.py`.
4. Run the real test and OIDC/CLI composition:

```sh
SUPABRICKS_UC093_RUNTIME=/absolute/uc-runtime \
SUPABRICKS_UC094_CONFIG=/absolute/private-config.json \
  cargo test --release --locked -p supabricks-local --lib isolated_execution_real_uc -- --ignored
cargo build --release --locked -p supabricks-local --bin supabricks
python e2e/native/identity/qualify.py --binary target/release/supabricks \
  --uc-runtime /absolute/uc-runtime --execution-config /absolute/private-config.json \
  --output /tmp/uc094-identity.json
```

The Rust test exercises actual UC authority, immutable file staging, the platform
execution manager, Jupyter, native packages, Arrow/Delta and two private Sail
instances. `kernel_checks.py` runs through the actual managed ipykernel. It
rejects forbidden filesystem/network/socket access and exhausts bounded scratch,
process and descriptor budgets. The open-file/query case confirms the kernel has
opened the file before destroying the outside renewal handle and checking that
Docker removes the entire sandbox. Tests do not substitute a successful fake
launcher. Portable tests separately exercise denied policy/audit/session states
and hostile manifest/Delta paths before launch.

The Keycloak fixture covers the real CLI/daemon dispatch and live identity
introspection. Another user cannot read an execution's results; disabling the
actor prevents renewal and stops its long notebook within 35 seconds.

Only the bounded JSON identity report is published by CI. Logs and synthetic
credentials remain private. Ordinary exit removes containers and staging. After
a killed test process, the independent renewal deadline removes containers;
inspect only `supabricks.execution=true` containers if diagnosing a failed host.
Operator/runtime inventories are verified on every launch. Use optimized builds
for qualification: debug SHA-256 hashing of the full release can exceed client
request deadlines and is not a runtime performance measurement.
