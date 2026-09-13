"""Qualification cleanup is part of success; diagnostics must not expose commands."""
import subprocess


def stop(report, command, *, env, label='runtime'):
    try:
        result = subprocess.run(command, env=env, capture_output=True, timeout=90)
        if result.returncode == 0:
            return
        detail = dict(resource=label, exit_code=result.returncode)
    except (subprocess.TimeoutExpired, OSError) as error:
        detail = dict(resource=label, error=type(error).__name__)
    report['status'] = 'failed'
    report.setdefault('cleanup_errors', []).append(detail)
