#!/usr/bin/env python3
"""Fetch checksum-pinned helper archives without installing or executing them."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request

from validate import ROOT, read_json, validate


def fetch(url, checksum, destination):
    if destination.exists():
        with destination.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == checksum:
                return
        raise ValueError(f"existing file has the wrong checksum: {destination}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            digest = hashlib.sha256()
            with urllib.request.urlopen(url, timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                    digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != checksum:
            raise ValueError(f"download checksum mismatch: {url}")
        # Do not replace an existing file if another fetch completed first.
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["linux-x86_64", "macos-arm64"])
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = read_json(ROOT / "components/components.lock.json")
    errors = validate(manifest)
    if errors:
        parser.error("; ".join(errors))
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for component in manifest["components"]:
        if component["id"] not in {"process-compose", "seaweedfs"}:
            continue
        artifact = next(a for a in component["artifacts"] if a["target"] == args.target)
        path = args.output / f"{component['id']}-{args.target}.tar.gz"
        fetch(artifact["url"], artifact["sha256"], path)
        results.append({"component": component["id"], "path": str(path),
                        "sha256": artifact["sha256"], "status": "checksum-verified"})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
