#!/usr/bin/env python3
"""Adapter retry/conflict tests; never mutate the developer's Codex configuration."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "setup", Path(__file__).with_name("setup-codex.py")
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class SetupTest(unittest.TestCase):
    def test_manual_and_apply_retry_without_overwriting_existing_settings(self):
        with tempfile.TemporaryDirectory(prefix="supabricks adapter ") as temp:
            root = Path(temp)
            (root / "supabricks.toml").write_text(
                'format_version = 1\nid = "66dceea5-7c94-4eb5-b28d-00450757ec37"\nname = "orders"\n'
            )
            binary = root / "binary with spaces"
            binary.touch()
            data = root / "data"
            manual = adapter.setup(binary, root, data)
            existing = []
            calls = []

            def run(argv, **kwargs):
                calls.append(argv)
                if argv == ["codex", "--version"]:
                    stdout = adapter.TESTED_CODEX
                elif argv[1:3] == ["mcp", "list"]:
                    stdout = json.dumps(existing)
                elif argv[1:3] == ["mcp", "get"]:
                    stdout = json.dumps(existing[0])
                else:
                    self.assertEqual(argv, manual["codex_add_argv"])
                    existing.append(
                        dict(
                            name=manual["server"],
                            enabled=True,
                            transport=dict(
                                type="stdio", **manual["mcpServers"][manual["server"]]
                            ),
                        )
                    )
                    stdout = "registered"
                return subprocess.CompletedProcess(argv, 0, stdout)

            self.assertEqual(
                adapter.setup(binary, root, data, True, run)["status"], "registered"
            )
            self.assertEqual(
                adapter.setup(binary, root, data, True, run)["status"], "unchanged"
            )
            self.assertEqual(sum(c[1:3] == ["mcp", "add"] for c in calls), 1)
            existing[0]["transport"]["args"] = ["wrong-worktree"]
            with self.assertRaises(ValueError):
                adapter.setup(binary, root, data, True, run)
            self.assertEqual(adapter.setup(binary, root, data), manual)


if __name__ == "__main__":
    unittest.main()
