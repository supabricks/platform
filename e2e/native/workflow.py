#!/usr/bin/env python3
"""P06 public CLI + MCP + orders HTTP workflow. No private SQL/state APIs."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from cell import Cell, wait

REPO = Path(__file__).resolve().parents[2]


class MCP:
    def __init__(self, cell, project):
        self.p = subprocess.Popen(
            [
                str(cell.binary),
                "mcp",
                "--data-dir",
                str(cell.root),
                "--project",
                str(project),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cell.env,
        )
        self.sequence = 0
        self.rpc(
            "initialize",
            dict(
                protocolVersion="2025-06-18",
                capabilities={},
                clientInfo=dict(name="workflow", version="1"),
            ),
        )
        self.p.stdin.write(
            json.dumps(dict(jsonrpc="2.0", method="notifications/initialized")) + "\n"
        )
        self.p.stdin.flush()

    def rpc(self, method, params):
        self.sequence += 1
        self.p.stdin.write(
            json.dumps(
                dict(jsonrpc="2.0", id=self.sequence, method=method, params=params)
            )
            + "\n"
        )
        self.p.stdin.flush()
        assert select.select([self.p.stdout], [], [], 50)[0], "MCP response deadline"
        response = json.loads(self.p.stdout.readline())
        assert response.get("id") == self.sequence and "error" not in response, response
        return response["result"]

    def tool(self, tool_name, **args):
        result = self.rpc("tools/call", dict(name=tool_name, arguments=args))
        assert not result.get("isError"), result
        assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
        return result["structuredContent"]

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


class Workflow(Cell):
    def cli(self, *args, code=0, project=None):
        cmd = [
            str(self.binary),
            *map(str, args),
            "--data-dir",
            str(self.root),
            "--project",
            str(project or self.worktree),
        ]
        p = subprocess.run(
            cmd, env=self.env, capture_output=True, text=True, timeout=110
        )
        assert p.returncode == code, f"CLI {args[:2]}: {p.returncode}: {p.stderr}"
        return json.loads(p.stdout if p.stdout.strip() else p.stderr)

    def sql_public(self, sql, branch="main", write=False, **kwargs):
        return self.cli(
            "sql",
            "--sql",
            sql,
            "--branch",
            branch,
            *(["--write"] if write else []),
            **kwargs,
        )

    def await_op(self, op):
        result = self.cli("operation", "wait", op["id"])
        assert result["status"] == "succeeded", result
        return result

    def exercise(self):
        self.worktree = self.root / "orders-app"
        shutil.copytree(REPO / "examples/orders", self.worktree)
        init = self.cli("init", "orders")
        assert self.cli("init", "orders") == init
        self.cli("up", "--bundle", self.bundle, "--helpers", self.helpers)
        assert self.cli("doctor")["healthy"]
        caps = self.cli("capabilities")
        assert caps["worktree"] == str(self.worktree) and caps["api_version"] == 1
        op = self.cli("database", "create", "main", "--key", "create-main")
        self.await_op(op)
        assert (
            self.cli("database", "create", "main", "--key", "create-main")["id"]
            == op["id"]
        )
        self.cli("branch", "use", "main")
        self.cli(
            "sql",
            "--file",
            self.worktree / "migrations/001-orders.sql",
            "--branch",
            "main",
            "--write",
        )
        self.sql_public(
            "INSERT INTO orders(customer,total_cents) VALUES ('Ada',1299)", write=True
        )
        uri = self.cli("connect")["uri"]
        self.checks.append(
            "CLI init/up/create with automatic ports, idempotent retry and observable operation progress"
        )

        # Run the actual sample app against the URI returned to an application.
        app = subprocess.Popen(
            [sys.executable, str(self.worktree / "app.py"), "--port", "0"],
            env=dict(self.env, DATABASE_URL=uri),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert select.select([app.stdout], [], [], 10)[0]
            url = app.stdout.readline().strip().split(" on ", 1)[1]
            with urllib.request.urlopen(url, timeout=10) as response:
                assert json.load(response)["orders"][0]["customer"] == "Ada"
            req = urllib.request.Request(
                url,
                json.dumps(dict(customer="Grace", total_cents=2500)).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                assert (
                    response.status == 201
                    and json.load(response)["total_cents"] == 2500
                )
        finally:
            app.terminate()
            app.wait(timeout=5)
        self.checks.append(
            "orders HTTP GET and parameterized POST through the stable application URI"
        )

        other = self.root / "second-worktree"
        other.mkdir()
        shutil.copy2(self.worktree / "supabricks.toml", other / "supabricks.toml")
        self.cli("connect", project=other, code=3)
        mcp = MCP(self, other)
        try:
            assert len(mcp.rpc("tools/list", {})["tools"]) == 16
            assert mcp.tool("capabilities")["worktree"] == str(other)
            branch = mcp.tool(
                "create_branch", name="migration", parent="main", key="fork-migration"
            )
            self.await_op(branch)
            mcp.tool("select_branch", branch="migration")
            mcp.tool(
                "sql",
                branch="migration",
                sql=(self.worktree / "migrations/002-status.sql").read_text(),
                read_only=False,
            )
            mcp.tool(
                "sql",
                branch="migration",
                sql="UPDATE orders SET status='paid' WHERE customer='Ada'",
                read_only=False,
            )
            assert mcp.tool(
                "sql", sql="SELECT status FROM orders WHERE customer='Ada'"
            )["rows"] == [["paid"]]
            assert all(r[2] != "status" for r in self.cli("catalog")["rows"])
            assert any(r[2] == "status" for r in mcp.tool("catalog")["rows"])
            assert self.cli("connect")["uri"] == uri
            # Both clients see the same lifecycle conflict and structured error.
            record = mcp.tool("get_branch", branch="migration")
            error = mcp.rpc(
                "tools/call",
                dict(
                    name="set_state",
                    arguments=dict(
                        branch="migration",
                        expected_revision=record["revision"] + 99,
                        desired="suspended",
                        key="stale",
                    ),
                ),
            )
            assert (
                error["isError"]
                and error["structuredContent"]["error"]["code"] == "conflict"
            )
            self.checks.append(
                "MCP branch/migration/catalog workflow isolates parent and independent worktree selections"
            )

            assert (
                self.sql_public("DELETE FROM orders", code=6)["error"]["code"]
                == "sql_error"
            )
            assert (
                self.sql_public("SELECT 1; DELETE FROM orders", write=True, code=6)[
                    "error"
                ]["code"]
                == "sql_error"
            )
            assert (
                self.cli("sql", "--sql", "SELECT 1", "--write", code=2)["error"]["code"]
                == "invalid_input"
            )
            self.sql_public("SELECT generate_series(1,201)", code=6)
            self.sql_public("SELECT repeat('x',300000)", code=6)
            self.sql_public("SELECT repeat('x',2000000)", code=5)
            self.cli(
                "sql", "--sql", "SELECT pg_sleep(2)", "--timeout-ms", "100", code=6
            )
            self.cli(
                "sql",
                "--sql",
                "SELECT set_config('statement_timeout','0',false), pg_sleep(3)",
                "--timeout-ms",
                "100",
                code=6,
            )
            self.sql_public(
                "INSERT INTO orders(customer,total_cents) SELECT 'limit',n FROM generate_series(1,201) n RETURNING id",
                write=True,
                code=6,
            )
            assert self.sql_public("SELECT count(*) FROM orders")["rows"] == [["2"]]
            self.checks.append(
                "read-only and single-statement enforcement; bounded rows, bytes, wire frames and wall-clock SQL time"
            )

            # Four long SQL calls consume only the bounded worker pool; lifecycle
            # metadata and diagnostics must still respond while they run.
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [
                    pool.submit(self.sql_public, "SELECT pg_sleep(3)") for _ in range(4)
                ]
                wait(lambda: self.cli("status")["sql_workers_active"] == 4, timeout=2)
                start = time.monotonic()
                assert self.cli("doctor")["healthy"]
                self.sql_public("SELECT 1", code=5)
                assert time.monotonic() - start < 2
                for future in futures:
                    future.result()
            wait(lambda: self.cli("status")["sql_workers_active"] == 0)
            self.checks.append(
                "four-worker admission limit; SQL cannot block daemon diagnostics or lifecycle work"
            )

            self.cli("branch", "suspend", "migration", "--wait")
            assert mcp.tool(
                "sql", sql="SELECT status FROM orders WHERE customer='Ada'"
            )["rows"] == [["paid"]]
            stopped = self.cli("down")
            assert stopped["data_retained"]
            self.cli("doctor", code=5)
            self.cli("up")
            assert self.cli("connect")["uri"] == uri
            assert mcp.tool(
                "sql", sql="SELECT status FROM orders WHERE customer='Ada'"
            )["rows"] == [["paid"]]
            record = mcp.tool("get_branch", branch="migration")
            deleted = mcp.tool(
                "delete_branch",
                branch=record["branch"]["id"],
                expected_revision=record["revision"],
                key="remove-migration",
            )
            self.await_op(deleted)
            self.cli("connect", project=other, code=3)
            assert self.sql_public("SELECT count(*) FROM orders")["rows"] == [["2"]]
            self.checks.append(
                "explicit suspension wakes through SQL; persistent MCP binding/selection and stable URI survive down/up; named deletion retains parent"
            )
        finally:
            mcp.close()
        self.cli("down")
        return dict(
            status="PASS",
            checks=self.checks,
            limits=[
                "engineering PG17 bundle; no public installer",
                "real coding-agent usability is recorded separately",
            ],
        )


def main():
    ap = argparse.ArgumentParser()
    for name in ["binary", "bundle", "helpers", "report"]:
        ap.add_argument("--" + name, required=True, type=Path)
    args = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sb-p06-", dir="/tmp")).resolve()
    os.chmod(root, 0o700)
    cell = Workflow(
        args.binary.resolve(), args.bundle.resolve(), args.helpers.resolve(), root
    )
    try:
        report = cell.exercise()
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    except BaseException:
        args.report.write_text(
            json.dumps(dict(status="FAIL", checks=cell.checks), indent=2) + "\n"
        )
        cell.diagnostics(args.report.with_suffix(".log"))
        print(f"P06 fixture retained at {root}", flush=True)
        raise
    finally:
        cell.close()
        subprocess.run(
            [str(cell.binary), "down", "--data-dir", str(root)],
            env=cell.env,
            capture_output=True,
            timeout=75,
        )


if __name__ == "__main__":
    main()
