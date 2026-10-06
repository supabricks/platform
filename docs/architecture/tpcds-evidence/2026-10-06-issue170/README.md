# EQ01 / #170 evidence

[Assessment](../../eq01-composite-keys.md).

- `package-01.json`: verified Python-only candidate over the frozen SP engineering
  baseline; three changed production sources and their checked-hash bytecode.
- `native-01/`: first installed composite correctness run; original seven TPC-DS
  schemas plus populated fixtures, real Sail queries and process restart.
- `installed-composite/`: the mandatory installed-release suite adapter, exercised
  locally on the same engineering overlay. This is not signed-archive evidence.
- `control-{1,2,3}-{baseline,candidate}/`: all six unchanged single-key controls,
  complete per-transaction samples, Docker commands and cleanup receipts.
- `control-summary.json`: descriptive means/maxima from those samples. All control
  fixture hashes are identical; runtime differences are limited to the package proof.
- `campaign.json`: sequential attempt order and exit codes. No replacements or
  discarded control runs; all passed. Native correctness precedes the timing pairs.
- `admission/`: repeat of the full EQ00 schema-admission matrix with candidate workers.
- `analytics-tests.txt`: all 138 analytical worker tests pass.
- `composite-tests.txt`: final five composite tests, including the expanded
  32,768-old/new-key filter case, pass.
- `evidence-tests.txt`: release evidence accepts both platforms and rejects missing
  composite checks or worker identities.
- `rejected-or-filter-tests.txt.gz`: retained development attempt which exited 139
  in Arrow while expanding 16,384 exact-tuple OR predicates. That implementation
  was replaced before candidate packaging; it is not part of the measured candidate.

The installed adapter contract test also passes in the qualification container.
No product data or credentials are included. Raw owned trial outputs remain under
`build/eq01-170/`. All trials use CPU affinity 0–7 and a 16 GiB container cap with
swap disabled, except ordinary worker unit tests. The final full-schema admission
probe runs after timing controls. No SP campaign was run or resumed.

Verify all retained receipts and text logs using `sha256sum -c SHA256SUMS` from
this directory. Linux/macOS exact archive CI remains separate; these local
engineering-overlay results do not imply those jobs have passed.
