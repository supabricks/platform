#!/usr/bin/env python3
"""UC06 browser over source-built UC and a qualified PG/Sail installed fixture."""
import argparse
from pathlib import Path
import subprocess
import tempfile

from service import installed_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["release", "binary", "uc-runtime", "console", "report"]:
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--node", default="node")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sb-uc06-", dir="/tmp")).resolve()
    print("UC06 fixture:", root, flush=True)
    console = args.console.resolve()
    installed_fixture(
        args.release.resolve(), args.binary.resolve(), args.uc_runtime.resolve(),
        root / "release", console / "dist",
    )
    subprocess.run([
        args.node, str(console / "scripts/qualify-catalog.mjs"),
        "--binary", str(root / "release/bin/supabricks"),
        "--root", str(root), "--report", str(args.report.resolve()),
    ], check=True)


if __name__ == "__main__":
    main()
