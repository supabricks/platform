#!/usr/bin/env python3
"""Verify an unpacked native bundle against platform's selected engine sources."""

import argparse
import hashlib
import os
from pathlib import Path

from validate import ROOT, read_json, validate


def verify(bundle, lock):
    bundle = bundle.resolve()
    manifest = read_json(bundle / "manifest.json")
    components = {c["id"]: c for c in lock["components"]}
    pair = lock["engine_pair"]
    expected = components[pair["neon"]]["selection"]["commit"]
    if manifest.get("neon_commit") != expected or manifest.get("neon_dirty") is not False:
        raise ValueError("bundle must come from the exact selected clean Neon commit")
    if manifest.get("postgres_major") != 17:
        raise ValueError("bundle must contain PG17")
    sources = manifest.get("sources", [])
    pg = [s for s in sources if s["path"] == pair["submodule_path"]]
    if len(pg) != 1 or pg[0]["commit"] != pair["gitlink"] or pg[0]["role"] != "runtime":
        raise ValueError("bundle Postgres source differs from the selected engine gitlink")
    inventory = manifest["bundle"]
    files, links = inventory["files"], inventory["symlinks"]
    required = {f"bin/{name}" for name in ("pageserver", "safekeeper", "storage_broker", "compute_ctl")}
    required |= {f"pg_install/v17/bin/{name}" for name in ("postgres", "psql", "initdb", "pg_ctl", "pg_dump")}
    if not required <= files.keys():
        raise ValueError("bundle lacks required native engine/Postgres binaries")
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob("*")
              if p.is_file() or p.is_symlink()} - {"manifest.json"}
    if actual != files.keys() | links.keys():
        raise ValueError("bundle file inventory differs from manifest")
    for name in files.keys() | links.keys():
        path = bundle / name
        if not path.resolve().is_relative_to(bundle):
            raise ValueError(f"bundle path escapes root: {name}")
        if name in links:
            if not path.is_symlink() or os.readlink(path) != links[name]:
                raise ValueError(f"symlink mismatch: {name}")
            if not path.exists():
                raise ValueError(f"dangling symlink: {name}")
        else:
            if path.is_symlink():
                raise ValueError(f"expected a regular file: {name}")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != files[name]:
                    raise ValueError(f"file checksum mismatch: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        lock = read_json(ROOT / "components/components.lock.json")
        errors = validate(lock)
        if errors:
            raise ValueError("; ".join(errors))
        verify(args.bundle, lock)
    except (KeyError, TypeError, ValueError, OSError) as error:
        parser.exit(1, f"ERROR: {error}\n")
    print("Native bundle matches the selected sources and recorded file hashes. Runtime qualification is separate.")


if __name__ == "__main__":
    main()
