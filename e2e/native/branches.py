#!/usr/bin/env python3
"""P04 real native branch, credential, TTL and interrupted teardown qualification."""

import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
import urllib.request
import urllib.error
from cell import Cell, wait, lsn


class BranchCell(Cell):
    def ports(self):
        sockets = [socket.socket() for _ in range(3)]
        try:
            for s in sockets:
                s.bind(("127.0.0.1", 0))
            return dict(
                zip(
                    ["sql", "external_http", "internal_http"],
                    [s.getsockname()[1] for s in sockets],
                )
            )
        finally:
            for s in sockets:
                s.close()

    def submit(self, mutation, key=None):
        return self.request(
            method="submit",
            project_id=self.project,
            key=key or str(uuid.uuid4()),
            mutation=mutation,
        )

    def operation(self, op):
        def done():
            current = self.request(method="operation", id=op["id"])
            if current["status"] == "failed":
                raise AssertionError(f"operation failed: {current['error']}")
            return current["status"] == "succeeded"

        wait(done, timeout=120)

    def get(self, b):
        return self.request(
            method="get_branch", project_id=self.project, id=b["branch"]["id"]
        )

    def connect(self, b):
        return self.request(
            method="connection", project_id=self.project, id=b["branch"]["id"]
        )

    def app_process(self, b, query, password=None, user=None):
        c = self.connect(b)
        env = dict(self.env, PGPASSWORD=c["password"] if password is None else password)
        return subprocess.Popen(
            [
                str(self.bundle / "pg_install/v17/bin/psql"),
                "-XAt",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                c["host"],
                "-p",
                str(c["port"]),
                "-U",
                user or c["username"],
                "-d",
                c["database"],
                "-c",
                query,
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def app(self, b, query, password=None, user=None):
        p = self.app_process(b, query, password, user)
        out, err = p.communicate(timeout=20)
        if p.returncode:
            raise RuntimeError(err.strip())
        return out.strip()

    def fork(self, parent, name, point=None):
        op = self.submit(
            dict(
                kind="branch_from",
                name=name,
                parent_id=parent["branch"]["id"],
                ports=self.ports(),
                point=point or dict(kind="head"),
            )
        )
        self.operation(op)
        return self.request(
            method="get_branch", project_id=self.project, id=op["branch_id"]
        )

    def deny(self, action):
        try:
            action()
        except RuntimeError:
            return
        raise AssertionError("protected action was accepted")

    def validate_generation(self, tenant):
        generation = self.request(method="status")["generation"]
        body = json.dumps(
            dict(
                tenants=[
                    dict(id=tenant, gen=generation),
                    dict(id=tenant, gen=generation - 1),
                    dict(id="0" * 32, gen=generation),
                ]
            )
        ).encode()
        url = f"http://127.0.0.1:{self.config['ports']['validator']}/validate"
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5)
        except urllib.error.HTTPError as e:
            assert e.code == 401
        else:
            raise AssertionError("validator accepted unauthenticated request")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": "Bearer " + self.config["validation_token"]},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            assert [t["valid"] for t in json.load(response)["tenants"]] == [
                True,
                False,
                False,
            ]

    def exercise(self):
        self.start()
        self.request(
            method="register_project",
            config=dict(format_version=1, id=self.project, name="branches"),
        )
        mutation = dict(kind="create_database", name="main", ports=self.ports())
        with ThreadPoolExecutor(max_workers=5) as pool:
            ops = list(
                pool.map(lambda _: self.submit(mutation, "create-main"), range(5))
            )
        assert len({op["id"] for op in ops}) == 1
        self.operation(ops[0])
        main = self.request(
            method="get_branch", project_id=self.project, id=ops[0]["branch_id"]
        )
        assert main["is_default"]
        self.validate_generation(main["branch"]["tenant_id"])
        assert len(self.request(method="list_branches", project_id=self.project)) == 1
        c = self.connect(main)
        assert c["username"] == "supabricks_owner"
        assert c["password"] != self.credentials(main)
        self.deny(lambda: self.app(main, "SELECT 1", password="wrong"))
        self.deny(lambda: self.app(main, "SELECT 1", user="cloud_admin"))
        self.deny(lambda: self.app(main, "SET ROLE cloud_admin"))
        self.deny(lambda: self.app(main, "SELECT pg_read_file('/etc/passwd')"))
        assert (
            self.app(
                main,
                "SELECT rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=current_user",
            )
            == "f|f|f|f|f"
        )
        self.checks.append(
            "concurrent duplicate database create; application role separated from private control credentials"
        )
        self.app(
            main,
            "CREATE TABLE events(id int PRIMARY KEY); INSERT INTO events VALUES (1),(2)",
        )
        acknowledged = self.app(main, "SELECT pg_current_wal_flush_lsn()")
        child = self.fork(main, "child")
        assert lsn(child["branch"]["ancestor_lsn"]) >= lsn(acknowledged)
        assert (
            self.app(child, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2"
        )
        self.app(main, "INSERT INTO events VALUES (3)")
        self.app(child, "INSERT INTO events VALUES (4)")
        assert (
            self.app(main, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2,3"
        )
        assert (
            self.app(child, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2,4"
        )
        grandchild = self.fork(child, "grandchild")
        assert (
            self.app(
                grandchild, "SELECT string_agg(id::text,',' ORDER BY id) FROM events"
            )
            == "1,2,4"
        )
        assert self.connect(child)["password"] != c["password"]
        self.deny(lambda: self.app(child, "SELECT 1", password=c["password"]))
        self.checks.append(
            "immediate committed write then exact head branch, parent/child isolation and branch-of-branch"
        )
        main = self.state(main, "suspended")
        asleep = self.fork(main, "from-suspended")
        wait(
            lambda: self.get(main)["endpoint"]["desired_state"] == "suspended"
            and self.get(main)["revision"] == self.get(main)["observed_revision"]
        )
        assert (
            self.app(asleep, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2,3"
        )
        self.checks.append(
            "suspended parent wakes under a durable internal pin and returns to suspension"
        )
        main = self.state(self.get(main), "running")
        exact = self.fork(
            main, "exact", dict(kind="lsn", lsn=child["branch"]["ancestor_lsn"])
        )
        assert (
            self.app(exact, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2"
        )
        self.app(main, "INSERT INTO events VALUES (5)")
        at = self.app(main, "SELECT clock_timestamp()")
        time.sleep(0.1)
        self.app(main, "INSERT INTO events VALUES (6)")
        historical = self.fork(
            main,
            "historical",
            dict(kind="time", timestamp=datetime.fromisoformat(at).isoformat()),
        )
        assert (
            self.app(
                historical, "SELECT string_agg(id::text,',' ORDER BY id) FROM events"
            )
            == "1,2,3,5"
        )
        self.checks.append(
            "explicit retained LSN and timestamp branches resolve to exact durable ancestor pins"
        )
        interrupted = self.submit(
            dict(
                kind="branch_from",
                name="interrupted-create",
                parent_id=main["branch"]["id"],
                ports=self.ports(),
                point=dict(kind="head"),
            )
        )

        def captured():
            with sqlite3.connect(
                f"file:{self.root}/state.sqlite3?mode=ro", uri=True
            ) as db:
                return db.execute(
                    "SELECT ancestor_lsn FROM branches WHERE id=?",
                    (interrupted["branch_id"],),
                ).fetchone()[0]

        deadline = time.monotonic() + 30
        while not (pinned := captured()):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        self.daemons[-1].kill()
        self.daemons[-1].wait(timeout=5)
        self.start()
        self.operation(interrupted)
        resumed = self.request(
            method="get_branch", project_id=self.project, id=interrupted["branch_id"]
        )
        assert resumed["branch"]["ancestor_lsn"] == pinned
        assert (
            self.app(resumed, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2,3,5,6"
        )
        self.checks.append(
            "owner SIGKILL after persisted head capture resumes creation at the original exact LSN"
        )
        bad = self.submit(
            dict(
                kind="branch_from",
                name="future-lsn",
                parent_id=main["branch"]["id"],
                ports=self.ports(),
                point=dict(kind="lsn", lsn="FFFFFFFF/FFFFFFF8"),
            )
        )
        wait(
            lambda: self.request(method="operation", id=bad["id"])["status"] == "failed"
        )
        b = self.request(
            method="get_branch", project_id=self.project, id=bad["branch_id"]
        )
        assert not b["timeline_created"]
        self.state(b, "deleted")
        self.deny(
            lambda: self.submit(
                dict(
                    kind="branch_from",
                    name="bad",
                    parent_id=main["branch"]["id"],
                    ports=self.ports(),
                    point=dict(kind="lsn", lsn="0/11"),
                )
            )
        )
        self.deny(lambda: self.state(child, "deleted"))
        self.deny(
            lambda: self.submit(
                dict(
                    kind="set_ttl",
                    branch_id=main["branch"]["id"],
                    expected_revision=self.get(main)["revision"],
                    expires_at_ms=int(time.time() * 1000) + 2000,
                )
            )
        )
        self.deny(lambda: self.state(main, "deleted"))
        # Stop the actual ingestion service and let a short operation deadline
        # expire. Resuming storage must not turn this into a stale branch.
        ps_pid = next(r["pid"] for r in self.records() if r["role"] == "pageserver")
        os.kill(ps_pid, signal.SIGSTOP)
        try:
            timed = self.submit(
                dict(
                    kind="branch_from",
                    name="ingestion-timeout",
                    parent_id=main["branch"]["id"],
                    ports=self.ports(),
                    point=dict(kind="head"),
                    timeout_ms=1000,
                ),
                "ingestion-timeout",
            )
            time.sleep(2)
        finally:
            os.kill(ps_pid, signal.SIGCONT)
        wait(
            lambda: self.request(method="operation", id=timed["id"])["status"]
            == "failed"
        )
        held = self.request(
            method="get_branch", project_id=self.project, id=timed["branch_id"]
        )
        assert not held["timeline_created"]
        assert not (
            self.root
            / "pageserver"
            / "tenants"
            / held["branch"]["tenant_id"]
            / "timelines"
            / held["branch"]["timeline_id"]
        ).exists()
        self.state(held, "deleted")
        self.checks.append(
            "ingestion service outage expires the branch operation without substituting an earlier boundary"
        )
        self.checks.append(
            "bad LSN fails without creating a timeline; parent and default-branch protections hold"
        )
        temporary = self.fork(main, "temporary")
        lease = self.request(
            method="acquire_lease",
            project_id=self.project,
            branch_id=temporary["branch"]["id"],
            holder="qualification",
            ttl_ms=60000,
        )
        existing = self.app_process(temporary, "SELECT pg_sleep(5); SELECT 42")
        self.submit(
            dict(
                kind="set_ttl",
                branch_id=temporary["branch"]["id"],
                expected_revision=temporary["revision"],
                expires_at_ms=int(time.time() * 1000) + 1500,
            )
        )
        wait(lambda: self.get(temporary)["expired"])
        self.deny(lambda: self.connect(temporary))
        self.deny(
            lambda: self.request(
                method="acquire_lease",
                project_id=self.project,
                branch_id=temporary["branch"]["id"],
                holder="late",
                ttl_ms=10000,
            )
        )
        self.request(method="release_lease", project_id=self.project, lease=lease)
        assert self.get(temporary)["endpoint"]["desired_state"] != "deleted"
        out, err = existing.communicate(timeout=15)
        assert existing.returncode == 0 and out.strip() == "42", err
        wait(
            lambda: self.get(temporary)["endpoint"]["desired_state"] == "deleted"
            and self.get(temporary)["ports"] is None
        )
        assert not (self.root / "computes" / temporary["endpoint"]["id"]).exists()
        self.checks.append(
            "TTL rejects new work, drains existing SQL and work leases, then reclaims the branch"
        )
        # Delete intent and cleanup must survive a killed owner.
        self.submit(
            dict(
                kind="set_state",
                branch_id=grandchild["branch"]["id"],
                expected_revision=grandchild["revision"],
                desired="deleted",
            )
        )
        self.daemons[-1].kill()
        self.daemons[-1].wait(timeout=5)
        self.start()
        wait(lambda: self.get(grandchild)["ports"] is None)
        assert not (
            self.root
            / "safekeeper"
            / grandchild["branch"]["tenant_id"]
            / grandchild["branch"]["timeline_id"]
        ).exists()
        assert not (self.root / "computes" / grandchild["endpoint"]["id"]).exists()
        assert (
            self.app(child, "SELECT string_agg(id::text,',' ORDER BY id) FROM events")
            == "1,2,4"
        )
        assert self.connect(main)["password"] == c["password"]
        self.checks.append(
            "interrupted ordered teardown replays; unrelated data and application credentials survive restart"
        )
        for b in [resumed, historical, exact, asleep, child]:
            self.state(self.get(b), "deleted")
        forced = self.submit(
            dict(
                kind="force_delete",
                branch_id=main["branch"]["id"],
                expected_revision=self.get(main)["revision"],
            )
        )
        self.operation(forced)
        assert not self.request(method="list_branches", project_id=self.project)
        remaining = [
            o["Key"]
            for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket="supabricks", Prefix="pageserver/"
            )
            for o in page.get("Contents", [])
        ]
        assert not [key for key in remaining if "/timelines/" in key], remaining
        self.checks.append(
            "authenticated generation validation rejects stale ownership; timeline deletion reclaims all timeline S3 objects"
        )
        self.stop()
        # Upgrade an actual stopped P03 runtime configuration without changing
        # any existing endpoint/storage port or credential.
        prior = dict(self.config)
        prior["version"] = 1
        prior.pop("validation_token")
        prior["ports"].pop("validator")
        (self.root / "runtime.json").write_text(json.dumps(prior))
        self.start()
        assert self.config["version"] == 2
        assert all(self.config["ports"][k] == v for k, v in prior["ports"].items())
        assert self.config["supervisor_token"] == prior["supervisor_token"]
        self.stop()
        self.checks.append(
            "P03 runtime configuration upgrades in place while preserving existing ports and credentials"
        )
        # Metadata-loss recovery is an explicit stopped-cell backup restore,
        # unlike ordinary compute restart or S3 layer reattachment.
        state = self.root / "state.sqlite3"
        backup = self.root / "saved-state.sqlite3"
        state.rename(backup)
        p = subprocess.run(
            [str(self.binary), "up", "--data-dir", str(self.root)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert p.returncode != 0
        assert not state.exists()
        backup.rename(state)
        self.start()
        self.stop()
        self.checks.append(
            "missing control state refuses startup; restoring the stopped-cell metadata backup permits restart"
        )
        return dict(
            status="PASS",
            checks=self.checks,
            limits=[
                "engineering PG17 artifacts",
                "head/LSN/time branches use retained history; no stale fallback",
                "stable connection gateway follows in P05",
            ],
        )


def main():
    ap = argparse.ArgumentParser()
    for name in ["binary", "bundle", "helpers", "report"]:
        ap.add_argument("--" + name, required=True, type=Path)
    args = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sb-p04-", dir="/tmp")).resolve()
    os.chmod(root, 0o700)
    cell = BranchCell(
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
        print(
            f"Failed P04 cell retained at {root}; completed checks: {cell.checks}",
            flush=True,
        )
        raise
    finally:
        cell.close()


if __name__ == "__main__":
    main()
