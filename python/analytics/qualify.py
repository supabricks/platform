#!/usr/bin/env python3
"""Run the synthetic fixture in a fresh process and preserve bounded evidence."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import tomllib

import psutil


ROOT = Path(__file__).resolve().parents[2]
ENV = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment():
    expected_python = (ENV / ".python-version").read_text().strip()
    if platform.python_version() != expected_python:
        raise RuntimeError(f"expected Python {expected_python}, got {platform.python_version()}")
    target = {("Linux", "x86_64"): "linux-x86_64",
              ("Darwin", "arm64"): "macos-arm64"}.get(
                  (platform.system(), platform.machine()))
    if target is None:
        raise RuntimeError("qualification supports Linux x86_64 and macOS arm64")
    lock = tomllib.loads((ENV / "uv.lock").read_text())
    expected = {p["name"]: p["version"] for p in lock["package"]
                if "registry" in p["source"]}
    normalize = lambda name: name.lower().replace("_", "-").replace(".", "-")
    installed = {normalize(d.metadata["Name"]): d.version
                 for d in importlib.metadata.distributions()}
    if installed != expected and "jupyter-server" in installed:
        # The notebook runtime is an exact qualified superset. Keep the analytical
        # lock and every existing version unchanged for data compatibility.
        from packaging.requirements import Requirement
        notebook = ENV.parent / "notebooks"
        superset = tomllib.loads((notebook / "uv.lock").read_text())
        versions = {p["name"]: p["version"] for p in superset["package"] if "registry" in p["source"]}
        if any(versions.get(name) != version for name, version in expected.items()):
            raise RuntimeError("notebook lock changed analytical package versions")
        requirements = [Requirement(line.split(chr(92))[0].strip())
                        for line in (notebook / "requirements.lock").read_text().splitlines()
                        if line and line[0].isalnum() and "==" in line]
        expected = {r.name: versions[r.name] for r in requirements if r.marker is None or r.marker.evaluate()}
    if installed != expected:
        raise RuntimeError(f"environment differs from lock: expected {expected}, got {installed}")
    inventory = json.loads((ROOT / "components/components.lock.json").read_text())
    for component in inventory["components"]:
        selection = component["selection"]
        if selection["kind"] == "package" and selection["name"] in expected:
            if selection["version"] != expected[selection["name"]]:
                raise RuntimeError(f"component inventory drift: {component['id']}")
    return target, installed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    report = {
        "schema_version": 1,
        "scope": "A00 synthetic component qualification; no Postgres integration or release packaging",
        "status": "FAIL",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_build": platform.python_build(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "input_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in (
            ENV / "pyproject.toml", ENV / ".python-version", ENV / "uv.lock",
            ENV / "requirements.lock", Path(__file__).resolve(),
            ENV / "read_delta.py",
            ROOT / "spikes/local-analytics/smoke.py")},
        "measurement_scope": {
            "startup": "child launch to first SQL result includes interpreter and imports; excludes dependency installation",
            "memory": "sampled sum of RSS for fixture Python process and descendants, including Sail, Spark client, Arrow and delta-rs; shared pages may be counted multiple times",
            "sampling_seconds": 0.01,
            "limits": "synthetic data, warm OS caches possible; not isolated Sail RSS, production capacity, or power-loss durability",
        },
    }
    child = None
    try:
        report["target"], report["versions"] = environment()
        with tempfile.TemporaryDirectory(prefix="supabricks-a00-") as tmp:
            worker_report = Path(tmp) / "worker.json"
            started = time.perf_counter()
            child = subprocess.Popen(
                [sys.executable, "-W", "error", str(ROOT / "spikes/local-analytics/smoke.py"),
                 "--output", str(worker_report), "--launch-time", str(started)],
                start_new_session=True,
            )
            process = psutil.Process(child.pid)
            peak = 0
            names = set()
            while child.poll() is None:
                if time.perf_counter() - started > args.timeout:
                    raise TimeoutError(f"fixture exceeded {args.timeout} seconds")
                rss = 0
                try:
                    processes = [process, *process.children(recursive=True)]
                except psutil.NoSuchProcess:
                    processes = []
                for item in processes:
                    try:
                        name = item.name()
                        names.add(name)
                        if name.lower().startswith("java"):
                            raise RuntimeError(f"unexpected JVM process: {name}")
                        rss += item.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, rss)
                time.sleep(0.01)
            report["wall_seconds"] = round(time.perf_counter() - started, 3)
            report["sampled_peak_process_tree_rss_mib"] = round(peak / 1024**2, 1)
            report["observed_process_names"] = sorted(names)
            report["worker_exit_code"] = child.returncode
            if worker_report.exists():
                report["fixture"] = json.loads(worker_report.read_text())
            if child.returncode != 0 or report.get("fixture", {}).get("status") != "PASS":
                raise RuntimeError("fixture failed; see worker output and fixture report")
            report["status"] = "PASS"
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if child is not None and child.poll() is None:
            import signal
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "report": str(args.output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
