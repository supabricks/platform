#!/usr/bin/env python3
"""Validate the component inventory offline; never download or execute artifacts."""

import argparse
import json
from pathlib import Path
import re
import sys

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path(__file__).parent / "schemas/components.schema.json"
REQUIRED_COMPONENTS = {
    "neon-engine", "postgres17", "process-compose", "seaweedfs", "sqlite",
    "python", "pysail", "pyspark-client", "deltalake", "pyarrow", "pandas",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def validate(manifest, root=ROOT, require_qualified=False):
    schema = read_json(SCHEMA)
    Draft202012Validator.check_schema(schema)
    errors = [
        f"{'/'.join(map(str, error.absolute_path)) or '/'}: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(manifest)
    ]
    if errors:
        return errors

    root = root.resolve()

    def evidence_exists(relative):
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            errors.append(f"evidence must be a file inside the repo: {relative}")

    components = {}
    for component in manifest["components"]:
        name = component["id"]
        if name in components:
            errors.append(f"duplicate component: {name}")
        components[name] = component
        artifacts = {}
        for artifact in component["artifacts"]:
            target = artifact["target"]
            if target in artifacts:
                errors.append(f"{name}: duplicate artifact target: {target}")
            artifacts[target] = artifact
            evidence_exists(artifact["provenance"])
        for target, qualification in component["qualification"].items():
            for evidence in qualification["evidence"]:
                evidence_exists(evidence)
            state = qualification["state"]
            if state == "qualified":
                if component["selection"]["kind"] == "unselected":
                    errors.append(f"{name}/{target}: qualified component has no pin")
                if target not in artifacts:
                    errors.append(f"{name}/{target}: qualified component has no artifact")
            if require_qualified and state != "qualified":
                errors.append(f"{name}/{target}: {state}; qualification required")

    for name in sorted(REQUIRED_COMPONENTS - components.keys()):
        errors.append(f"missing initial component: {name}")

    pair = manifest["engine_pair"]
    for field, expected in (("neon", "neon-engine"), ("postgres", "postgres17")):
        name = pair[field]
        if name != expected or name not in components:
            errors.append(f"engine_pair/{field}: must reference {expected}")
        elif components[name]["selection"]["kind"] != "git":
            errors.append(f"{name}: engine source must be an exact git commit")
    postgres = components.get(pair["postgres"], {})
    if postgres.get("selection", {}).get("commit") != pair["gitlink"]:
        errors.append("engine_pair: selected Postgres commit differs from Neon gitlink")

    # A successful native probe applies only to the exact source pair and host
    # in its report. Updating a pin must not silently carry old evidence forward.
    for name in ("neon-engine", "postgres17"):
        for target, qualification in components.get(name, {}).get("qualification", {}).items():
            for evidence in qualification["evidence"]:
                if evidence != "components/provenance/native-linux.json":
                    continue
                path = (root / evidence).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    continue  # Already reported by evidence_exists.
                native = read_json(path)
                build, smoke = native.get("build", {}), native.get("smoke", {})
                neon = components.get("neon-engine", {}).get("selection", {}).get("commit")
                expected_pg = {"path": pair["submodule_path"], "commit": pair["gitlink"], "role": "runtime"}
                if (qualification["state"] != "probe-passed" or native.get("target") != target
                        or native.get("qualification") != "probe-passed"
                        or build.get("neon_commit") != neon or build.get("neon_dirty") is not False
                        or build.get("postgres_major") != 17
                        or build.get("postgres_regression") != "passed"
                        or expected_pg not in build.get("sources", [])
                        or smoke.get("status") != "PASS" or smoke.get("neon_commit") != neon
                        or smoke.get("sources") != build.get("sources")):
                    errors.append(f"{name}/{target}: claim exceeds the recorded native probe")

    # The chart intentionally keeps name+digest on one line for its installer.
    # Check the recorded legacy image identities without importing YAML or
    # treating them as evidence about the native source combination.
    chart = (root / manifest["legacy_images"]["source"]).read_text()
    chart_images = dict(re.findall(
        r'^  (\w+): \{name: "[^"]+", digest: "([^\"]+@sha256:[0-9a-f]{64})"\}',
        chart, re.MULTILINE,
    ))
    locked_images = {}
    for entry in manifest["legacy_images"]["images"]:
        if entry["name"] in locked_images:
            errors.append(f"duplicate legacy image: {entry['name']}")
        locked_images[entry["name"]] = entry["reference"]
    if locked_images != chart_images:
        errors.append("legacy_images: digest inventory differs from chart/values.yaml")

    # Probe claims must continue to name the actual exercised package versions.
    report = read_json(root / "spikes/local-analytics/result.json")
    requirements = (root / "spikes/local-analytics/requirements.txt").read_text().splitlines()
    pins = dict(line.split("==", 1) for line in requirements if "==" in line)
    for name, component in components.items():
        for target, qualification in component["qualification"].items():
            if "spikes/local-analytics/result.json" not in qualification["evidence"]:
                continue
            selection = component["selection"]
            version = selection.get("version")
            if (qualification["state"] != "probe-passed" or target != "linux-x86_64"
                    or report.get("status") != "PASS" or selection.get("name") != name
                    or version is None or report.get("versions", {}).get(name) != version
                    or pins.get(name) != version):
                errors.append(f"{name}/{target}: claim exceeds the recorded analytic probe")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", type=Path,
                        default=ROOT / "components/components.lock.json")
    parser.add_argument("--require-qualified", action="store_true",
                        help="also reject any target without native qualification")
    args = parser.parse_args()
    try:
        errors = validate(read_json(args.manifest), require_qualified=args.require_qualified)
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Component inventory valid." + (
        " All targets have qualification records." if args.require_qualified
        else " Native release qualification is a separate gate."
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
