#!/usr/bin/env python3
"""Prepare an explicit worktree-bound MCP registration; --apply uses Codex's CLI."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tomllib
import uuid

TESTED_CODEX = "codex-cli 0.153.4"


def configuration(binary, project, data):
    binary, project, data = (
        binary.resolve(strict=True),
        project.resolve(strict=True),
        data.resolve(),
    )
    config = tomllib.loads((project / "supabricks.toml").read_text())
    if config.get("format_version") != 1:
        raise ValueError("unsupported project format")
    uuid.UUID(config["id"])
    digest = hashlib.sha256((str(project) + "\0" + str(data)).encode()).hexdigest()[:12]
    server = "supabricks-" + digest
    transport = dict(
        command=str(binary),
        args=["mcp", "--project", str(project), "--data-dir", str(data)],
    )
    return server, transport


def setup(binary, project, data, apply=False, run=subprocess.run):
    server, transport = configuration(binary, project, data)
    argv = [
        "codex",
        "mcp",
        "add",
        server,
        "--",
        transport["command"],
        *transport["args"],
    ]
    result = dict(
        server=server,
        mcpServers={server: transport},
        codex_add_argv=argv,
        status="manual",
        tested_codex=TESTED_CODEX,
        next="Register using codex_add_argv or generic MCP settings, then open this worktree and inspect /mcp. Host trust and approvals remain under your control.",
    )
    if not apply:
        return result
    version = run(
        ["codex", "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if version != TESTED_CODEX:
        raise ValueError(
            f"adapter qualified on {TESTED_CODEX}; installed {version}. Use manual configuration for this version"
        )
    # Inspect the full list rather than interpreting all get failures as absence.
    entries = json.loads(
        run(
            ["codex", "mcp", "list", "--json"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    existing = next((e for e in entries if e["name"] == server), None)
    if existing:
        existing = json.loads(
            run(
                ["codex", "mcp", "get", server, "--json"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        configured = existing.get("transport", {})
        if (
            configured.get("type") != "stdio"
            or configured.get("command") != transport["command"]
            or configured.get("args") != transport["args"]
            or not existing.get("enabled", True)
            or configured.get("env")
            or configured.get("env_vars")
            or configured.get("cwd")
        ):
            raise ValueError(
                f"{server} already has different settings; inspect it with codex mcp get before changing it"
            )
        result["status"] = "unchanged"
    else:
        run(argv, capture_output=True, text=True, check=True)
        result["status"] = "registered"
    result["codex_version"] = version
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ["binary", "project", "data-dir"]:
        parser.add_argument("--" + arg, required=True, type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="register with the installed Codex CLI; default only prints reviewable configuration",
    )
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                setup(args.binary, args.project, args.data_dir, args.apply), indent=2
            )
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Codex setup failed: {error}\n")


if __name__ == "__main__":
    main()
