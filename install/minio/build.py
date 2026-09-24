#!/usr/bin/env python3
"""Build the two pinned demo images; Docker layer caching avoids repeat compilation."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main():
    directory = ROOT / "install/minio"
    entries = json.loads((directory / "sources.json").read_text())
    chart = (ROOT / "chart/values.yaml").read_text()
    for entry in entries:
        name, commit, checksum = entry["name"], entry["commit"], entry["sha256"]
        if name not in ("minio", "mc") or not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("invalid demo source pin")
        match = re.search(r'^  ' + name + r': \{name: "([^\"]+)", digest: "source-sha256:' + checksum + r'"\}', chart, re.M)
        if not match:
            raise ValueError("chart and demo source pin differ: " + name)
        subprocess.run([
            "docker", "build", "--build-arg", "COMPONENT=" + name,
            "--build-arg", "COMMIT=" + commit, "--build-arg", "SOURCE_SHA256=" + checksum,
            "--label", "io.supabricks.demo.recipe=" + hashlib.sha256((directory / "Dockerfile").read_bytes()).hexdigest(),
            "--tag", match[1], str(directory),
        ], check=True)


if __name__ == "__main__":
    main()
