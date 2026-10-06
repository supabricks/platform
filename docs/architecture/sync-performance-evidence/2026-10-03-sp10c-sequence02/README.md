# SP10c sequence 02 launch evidence

## Qualification sequence 02

The remaining qualification uses clean frozen harness `3fdebb0` (`harness-07`),
including continuous quiet-host evidence. The completed 108-fixture owner
correction is reviewed and archived; its original campaign was not restarted.

The new immutable configurations are `sequence-config-02.json` and the matching
observer-bridge, engine-comparison and product-comparison `-config-02.json` files.
Packages are corrected SQLite owner (`sqlite-owner-fix-runtime-01`), corrected
SQLite owner plus markers (`sqlite-owner-runtime-02`), corrected RocksDB owner
plus markers (`rocks-runtime-04`), and best direct SQLite plus markers
(`sqlite-direct-runtime-01`). Both owner arms share identical owner, worker and
marker source hashes. All marker arms share the same marker source. Source
revisions and package/binary identities are recorded separately from the harness.

The sequence contains three 108-fixture campaigns, then three 30-minute sustained
runs: **327 fixtures** in total, approximately 21–22 hours on an uncontended host.
It begins with the observer bridge, then same-interface engine comparison, then
comparison against direct SQLite. Each campaign retains component, lifecycle,
observer, profiler and matched pipeline controls. The qualified input is 1,250
changed rows/s with eight source clients; historical four-client cells remain
for comparison and are not treated as proof of offered-load capacity.

Service: `supabricks-sp10c-sequence-02.service`. Evidence root:
`build/sp10c-20261003/sequence-02`. The controller reports a ten-second heartbeat,
stops on fixture/cleanup/identity failures, and preserves rejected evidence.
Fresh continuous host evidence carries quiet credit across phase boundaries;
activity or sampling gaps reset it. This is the first full integration of #155.
A completed service still requires receipt/hash review and an explicit measured
keep/reject decision. Launch does not establish RocksDB speedup or adoption.

The rebuilt SQLite marker package passed bounded reads, full lifecycle and observer off/on screens with clean descendant cleanup. The RocksDB package screens are archived in `../2026-10-03-rocks-accounting/`. All 83 performance harness tests passed. Screens establish launch prerequisites, not pipeline capacity.
