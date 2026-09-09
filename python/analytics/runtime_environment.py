"""Exact installed worker environment; A00's historical qualifier stays immutable."""
import importlib.metadata
import json
from pathlib import Path
import platform
import tomllib

ROOT = Path(__file__).resolve().parents[2]
ENV = Path(__file__).resolve().parent


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
