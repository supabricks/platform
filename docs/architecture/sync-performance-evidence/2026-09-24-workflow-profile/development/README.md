# Instrumentation development attempts

These attempts are retained for provenance and are excluded from the final matched matrix and controls.

- Pilot 01 (15 seconds, 4 CPUs / 50 rows/s): invalid. The diagnostic SQLite connection wrapper accidentally captured a rebound closure variable, blocking setup; fixed and covered by a regression test.
- Pilot 02: complete with Python/Rust spans; native sync probe not yet present.
- Pilot 03: complete with native sync probe. Per-COMMIT attribution and stable worker context IDs were added afterward. Exact package hashes and raw pilot reports are retained.
- Series 01: host preflight only; stopped before any trial to correct process-role matching and add batch UUID correlation. No performance outcome was produced.
- Series 02: six completed activation controls, five completed formal low-load profiles, and one invalid collector attempt. That final fixture passed table equality but the monitor stopped with `AccessDenied` while sampling a process. The controller halted. The first eleven results remain supplemental and are not pooled with the replacement series. All owned descendants were stopped, including the invalid attempt.
- The collector fix retains isolated process sampling denials, rejects persistent denials or denial for the daemon, and retains partial structured observations on monitor failure. All six controls and twelve matrix trials restart under the corrected collector; the diagnostic runtime package is unchanged.

`series-02/*/raw-reports.json.gz` contains original matrix, trial and cleanup records. Valid profiles are retained separately; the failed collector's stopped worker streams are marked partial. Private service logs/configuration and source row contents are excluded. The failed attempt's original `include_in_quiet_comparison` flag means no build overlap was observed; it does **not** override its invalid `status=error` or include it in final results.
