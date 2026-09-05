#!/usr/bin/env python3
"""Fail if the portable crates acquire operator, Kubernetes or UI dependencies."""
import pathlib
import subprocess

root = pathlib.Path(__file__).resolve().parent.parent
for package in ("supabricks-core", "supabricks-local"):
    result = subprocess.run(
        ["cargo", "tree", "--locked", "-p", package, "--all-features", "--target", "all",
         "--edges", "normal,build,dev", "--prefix", "none", "--format", "{p}"],
        cwd=root, text=True, capture_output=True, check=True,
    )
    names = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
    forbidden = sorted(name for name in names if name in {
        "sspc-operator", "k8s-openapi", "rust-embed", "rust-embed-impl", "rust-embed-utils"
    } or name == "kube" or name.startswith("kube-"))
    if forbidden:
        raise SystemExit(f"{package} is not portable: {', '.join(forbidden)}")
    print(f"{package}: {len(names)} dependency packages; no operator, Kubernetes or UI embedding")
