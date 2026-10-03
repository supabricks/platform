# Owner replacement correction validation

Production changes are limited to capture owner authority reads and startup contention classification. The accepted SP10b package fails the deterministic atomic replacement reproduction; the fixed reader returns only the current authority version.

- 130 analytics tests pass, including file replacement, revocation, bounded deferral, startup and preserved source history.
- 16 existing/new IPC tests pass against the experimental RocksDB backend.
- The complete RocksDB suite previously failed a resource-cycle physical-file check (#154). Ten diagnostic repeats did not reproduce it. The failed suite remains evidence; RocksDB adoption is not qualified.
- Initial test runs (`owner-fix-analytics-01.log`, `owner-fix-targeted-01.log`) exposed recursion in the test fault injector because patching os.fstat also intercepted atomic()'s temporary-file validation. The injector now selects only the target inode, and bounded retry counts are asserted. These original local logs are retained and fingerprinted in local-artifacts.json.

These are correctness tests, not throughput measurements. The SQLite-only correction requires its own installed screens and matched performance campaign before new engine-comparison packages are frozen. The original SP10c sequence remains stopped and unchanged.
