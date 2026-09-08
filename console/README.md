# Supabricks local console

C01 provides the packaged local project/branch overview. It is separate from the
Kubernetes UI in `../ui`. Runtime commands and browser support are documented in
the [console runbook](../docs/handbook/local-console.md); the
[C01 contract](../docs/architecture/c01-console.md) describes authentication,
ownership and qualification.

Build with Node 20.19+ (Node 22 in CI):

```sh
npm ci --prefix console --no-audit --no-fund
npm run build --prefix console
```

The production build emits `dist/console.json` and hashed assets. Native assembly
ships them alongside the executable. No Node server runs in the product.

Run the real browser workflow against a source binary and qualified native parts:

```sh
npm exec --prefix console -- playwright install chromium
node console/scripts/qualify.mjs --binary target/debug/supabricks \
  --bundle /absolute/native-engine --helpers /absolute/helpers \
  --report /tmp/console-report.json --screenshot /tmp/console-overview.png
```

The harness creates and cleans a new private `/tmp/sb-c01-*` cell. It does not use
the default user data root. CI's installed mode goes through
`install/native/qualify_console.py` and denies external networking. The local
source command restricts browser page requests but does not isolate host networking.
