#!/usr/bin/env python3
"""P05 native driver, listener, wake and suspension qualification."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from branches import BranchCell
from cell import wait, lsn


class ConnectionCell(BranchCell):
    def count(self, b):
        with sqlite3.connect(f"file:{self.root}/state.sqlite3?mode=ro", uri=True) as db:
            return db.execute(
                "SELECT count(*) FROM connection_leases WHERE branch_id=?",
                (b["branch"]["id"],),
            ).fetchone()[0]

    def idle(self, b):
        wait(lambda: self.count(b) == 0)

    def db(self, c, **kwargs):
        return psycopg.connect(
            host=c["host"],
            port=c["port"],
            user=c["username"],
            password=c["password"],
            dbname=c["database"],
            connect_timeout=20,
            autocommit=True,
            **kwargs,
        )

    def query(self, c, sql="SELECT count(*) FROM data"):
        with self.db(c) as db:
            return db.execute(sql).fetchone()[0]

    def create(self, name):
        op = self.submit(dict(kind="create_database", name=name, ports=self.ports()))
        self.operation(op)
        return self.request(
            method="get_branch", project_id=self.project, id=op["branch_id"]
        )

    def suspend(self, b):
        self.idle(b)
        b = self.state(self.get(b), "suspended")
        assert b["suspend_lsn"] and b["suspend_revision"] == b["revision"]
        assert not (self.root / "computes" / b["endpoint"]["id"]).exists()
        assert not (self.root / "tmp" / b["endpoint"]["id"]).exists()
        return b

    def certificate(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        cert_path = self.root / "server.crt"
        key_path = self.root / "server.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        os.chmod(key_path, 0o600)
        return dict(certificate=str(cert_path), key=str(key_path))

    def exercise(self):
        self.start()
        self.request(
            method="register_project",
            config=dict(format_version=1, id=self.project, name="connections"),
        )
        main = self.create("main")
        c = self.connect(main)
        assert c["port"] != main["ports"]["sql"] and c["uri"].startswith(
            "postgresql://"
        )
        with self.db(c) as db:
            db.execute("CREATE TABLE data(id int PRIMARY KEY, label text)")
            with db.transaction():
                db.execute("INSERT INTO data VALUES (%s,%s)", (1, "committed"))
            try:
                with db.transaction():
                    db.execute("INSERT INTO data VALUES (2,'rolled back')")
                    raise ValueError("rollback fixture")
            except ValueError:
                pass
            for _ in range(3):
                assert db.execute(
                    "SELECT label FROM data WHERE id=%s", (1,), prepare=True
                ).fetchone() == ("committed",)
            with db.cursor().copy("COPY data FROM STDIN") as copy:
                copy.write_row((3, "copied"))
                copy.write_row((4, "streamed"))
            with db.cursor().copy(
                "COPY (SELECT id FROM data ORDER BY id) TO STDOUT"
            ) as copy:
                assert b"".join(bytes(block) for block in copy) == b"1\n3\n4\n"
            point = str(db.execute("SELECT pg_current_wal_flush_lsn()").fetchone()[0])
        for changes in [dict(password="wrong"), dict(user="cloud_admin")]:
            options = dict(
                host=c["host"],
                port=c["port"],
                user=c["username"],
                password=c["password"],
                dbname=c["database"],
                connect_timeout=5,
            )
            options.update(changes)
            try:
                psycopg.connect(**options)
            except psycopg.OperationalError:
                pass
            else:
                raise AssertionError("wrong credentials accepted")
        assert self.app(
            main,
            "PREPARE one(int) AS SELECT $1; EXECUTE one(42); COPY (SELECT 7) TO STDOUT",
        ).endswith("42\n7")
        self.checks.append(
            "psql and psycopg auth, transactions, prepared statements and COPY pass through the stable listener"
        )
        with self.db(c, application_name="p05-cancel") as db:
            cancelled = []

            def query():
                try:
                    db.execute("SELECT pg_sleep(30)")
                except psycopg.errors.QueryCanceled:
                    cancelled.append(True)

            worker = threading.Thread(target=query)
            worker.start()
            wait(
                lambda: self.sql(
                    main,
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name='p05-cancel' AND state='active'",
                )
                == "1"
            )
            db.cancel_safe(timeout=5)
            worker.join(timeout=10)
            assert cancelled and not worker.is_alive()
            assert db.execute("SELECT 42").fetchone() == (42,)
        node = dict(
            host=c["host"],
            port=c["port"],
            user=c["username"],
            password=c["password"],
            database=c["database"],
            connectionTimeoutMillis=20000,
        )
        run = subprocess.run(
            ["node", str(Path(__file__).parent / "drivers/pg.mjs")],
            input=json.dumps(node),
            text=True,
            capture_output=True,
            timeout=45,
        )
        assert run.returncode == 0, run.stderr
        self.checks.append(
            "Python and Node cancellation use PostgreSQL CancelRequest; Node pg also passes prepared queries and streaming COPY"
        )
        main = self.suspend(main)
        assert lsn(main["suspend_lsn"]) >= lsn(point)
        assert self.connect(main) == c
        with ThreadPoolExecutor(max_workers=24) as pool:
            assert list(pool.map(lambda _: self.query(c), range(24))) == [3] * 24
        ready = self.get(main)
        assert ready["revision"] == main["revision"] + 1
        assert (
            len(
                [
                    r
                    for r in self.records()
                    if r["branch"] and r["branch"][0] == main["branch"]["id"]
                ]
            )
            == 1
        )
        self.checks.append(
            "24 concurrent connections deduplicate wake and see committed data through an unchanged URI"
        )
        self.idle(main)
        raw = []
        maximum = self.request(method="status")["gateway"]["max_connections_per_branch"]
        try:
            for _ in range(maximum):
                client = socket.create_connection((c["host"], c["port"]), timeout=5)
                raw.append(client)
                client.sendall(b"\x00\x00\x00\x08\x04\xd2\x16/")
                assert client.recv(1) == b"N"
            wait(lambda: self.count(main) == maximum)
            extra = socket.create_connection((c["host"], c["port"]), timeout=5)
            try:
                extra.sendall(b"\x00\x00\x00\x08\x04\xd2\x16/")
                try:
                    assert extra.recv(1) == b""
                except ConnectionResetError:
                    pass
            finally:
                extra.close()
        finally:
            for client in raw:
                client.close()
        self.idle(main)
        self.checks.append(
            "the per-branch connection limit rejects overflow without leaking connection leases"
        )
        # An idle pooled connection is still work and prevents suspension/deletion.
        pooled = self.create("pooled")
        pc = self.connect(pooled)
        db = self.db(pc)
        wait(lambda: self.count(pooled) == 1)
        revision = self.get(pooled)["revision"]
        noop = self.submit(
            dict(
                kind="set_state",
                branch_id=pooled["branch"]["id"],
                expected_revision=revision,
                desired="running",
            )
        )
        self.operation(noop)
        assert self.get(pooled)["revision"] == revision
        victim = next(
            r["pid"]
            for r in self.records()
            if r["branch"] and r["branch"][0] == pooled["branch"]["id"]
        )
        os.kill(victim, signal.SIGKILL)
        wait(lambda: self.count(pooled) == 0)
        try:
            db.execute("SELECT 1")
        except psycopg.OperationalError:
            pass
        else:
            raise AssertionError("dead compute retained an idle relay")
        db.close()
        wait(lambda: self.query(pc, "SELECT 42") == 42)
        self.idle(pooled)
        db = self.db(pc)
        wait(lambda: self.count(pooled) == 1)
        self.deny(lambda: self.state(pooled, "suspended"))
        self.deny(lambda: self.state(pooled, "deleted"))
        assert db.execute("SELECT 42").fetchone() == (42,)
        lease = self.request(
            method="acquire_lease",
            project_id=self.project,
            branch_id=pooled["branch"]["id"],
            holder="internal-export",
            ttl_ms=60000,
        )
        self.submit(
            dict(
                kind="set_ttl",
                branch_id=pooled["branch"]["id"],
                expected_revision=pooled["revision"],
                expires_at_ms=int(time.time() * 1000) + 1200,
            )
        )
        wait(lambda: self.get(pooled)["expired"])
        try:
            self.db(pc)
        except psycopg.OperationalError:
            pass
        else:
            raise AssertionError("expired branch accepted a connection")
        assert db.execute("SELECT 43").fetchone() == (43,)
        db.close()
        self.idle(pooled)
        assert self.get(pooled)["endpoint"]["desired_state"] != "deleted"
        self.request(method="release_lease", project_id=self.project, lease=lease)
        wait(lambda: self.get(pooled)["ports"] is None)
        self.checks.append(
            "idle pools protect suspension; compute death clears dead relays; TTL drains sessions and internal leases"
        )
        self.idle(main)
        marker = (
            self.root / "computes" / main["endpoint"]["id"] / "old-directory-marker"
        )
        marker.write_text("must be retired")
        current = self.get(main)
        op = self.submit(
            dict(
                kind="set_state",
                branch_id=current["branch"]["id"],
                expected_revision=current["revision"],
                desired="suspended",
            )
        )
        assert self.query(c) == 3
        self.operation(op)
        assert self.get(main)["revision"] == current["revision"] + 2
        assert not marker.exists()
        self.checks.append(
            "accept racing a committed suspend waits for retirement, then wakes a fresh compute directory"
        )
        # Exercise both sides of the durable termination receipt.
        for after_receipt in [False, True]:
            self.idle(main)
            current = self.get(main)
            op = self.submit(
                dict(
                    kind="set_state",
                    branch_id=current["branch"]["id"],
                    expected_revision=current["revision"],
                    desired="suspended",
                )
            )
            if after_receipt:

                def captured():
                    with sqlite3.connect(
                        f"file:{self.root}/state.sqlite3?mode=ro", uri=True
                    ) as db:
                        return db.execute(
                            "SELECT suspend_lsn FROM branches WHERE id=? AND suspend_revision=?",
                            (main["branch"]["id"], op["revision"]),
                        ).fetchone()

                deadline = time.monotonic() + 30
                while not captured():
                    assert time.monotonic() < deadline
                    time.sleep(0.005)
            self.daemons[-1].kill()
            self.daemons[-1].wait(timeout=5)
            self.start()
            self.operation(op)
            assert not (self.root / "computes" / main["endpoint"]["id"]).exists()
            assert self.connect(main) == c
            assert self.query(c) == 3
        self.checks.append(
            "SIGKILL before and after the termination receipt preserves the flush boundary, cleanup and stable address"
        )
        self.idle(main)
        main = self.suspend(main)
        self.stop()
        # An unrelated listener on the persisted address must prevent startup.
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind((c["host"], c["port"]))
        blocker.listen()
        try:
            run = subprocess.run(
                [str(self.binary), "up", "--data-dir", str(self.root)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert run.returncode != 0
            assert (
                f"listener port {c['port']} is unavailable"
                in (self.root / "daemon.log").read_text()
            )
            with sqlite3.connect(
                f"file:{self.root}/state.sqlite3?mode=ro", uri=True
            ) as db:
                assert db.execute(
                    "SELECT port FROM connection_endpoints WHERE branch_id=?",
                    (main["branch"]["id"],),
                ).fetchone() == (c["port"],)
        finally:
            blocker.close()
        # A short test deadline, then stop storage ingestion to hold wake pending.
        config = json.loads((self.root / "runtime.json").read_text())
        config["connection_startup_timeout_ms"] = 1000
        (self.root / "runtime.json").write_text(json.dumps(config))
        self.start()
        ps_pid = next(r["pid"] for r in self.records() if r["role"] == "pageserver")
        os.kill(ps_pid, signal.SIGSTOP)
        try:
            raw = socket.create_connection((c["host"], c["port"]), timeout=5)
            raw.sendall(b"\x00\x00\x00\x08\x04\xd2\x16/")  # SSLRequest
            raw.close()
            timed = socket.create_connection((c["host"], c["port"]), timeout=5)
            timed.settimeout(10)
            timed.sendall(b"\x00\x00\x00\x08\x04\xd2\x16/")
            started = time.monotonic()
            try:
                assert timed.recv(1) == b""
            except ConnectionResetError:
                pass
            assert time.monotonic() - started < 8
            timed.close()
        finally:
            os.kill(ps_pid, signal.SIGCONT)
        self.idle(main)
        # Restore the normal startup deadline before qualifying TLS across wake.
        self.stop()
        config = json.loads((self.root / "runtime.json").read_text())
        config["connection_startup_timeout_ms"] = 30000
        config["compute_tls"] = self.certificate()
        (self.root / "runtime.json").write_text(json.dumps(config))
        self.start()
        with self.db(
            c, sslmode="verify-full", sslrootcert=config["compute_tls"]["certificate"]
        ) as db:
            assert db.execute(
                "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
            ).fetchone() == (True,)
            assert db.execute("SELECT count(*) FROM data").fetchone() == (3,)
        self.checks.append(
            "persisted-port conflicts fail closed; disconnect and startup timeout release wake leases; configured TLS passes end to end"
        )
        self.idle(main)
        main = self.suspend(main)
        with self.db(
            c, sslmode="verify-full", sslrootcert=config["compute_tls"]["certificate"]
        ) as db:
            assert db.execute("SELECT count(*) FROM data").fetchone() == (3,)
        self.idle(main)
        # Force is a distinct action and closes active client sockets.
        db = self.db(c)
        op = self.submit(
            dict(
                kind="force_delete",
                branch_id=main["branch"]["id"],
                expected_revision=self.get(main)["revision"],
            )
        )
        self.operation(op)
        try:
            db.execute("SELECT 1")
        except psycopg.OperationalError:
            pass
        else:
            raise AssertionError("forced deletion left a client connected")
        db.close()
        self.idle(main)
        self.checks.append(
            "TLS remains configured across fresh-directory wake; explicit forced deletion closes active sockets"
        )
        self.stop()
        return dict(
            status="PASS",
            checks=self.checks,
            limits=[
                "engineering PG17.8 artifacts",
                "single-user loopback byte relay, no pooling",
                "manual safe suspension; automatic idle policy is not configured",
            ],
        )


def main():
    ap = argparse.ArgumentParser()
    for name in ["binary", "bundle", "helpers", "report"]:
        ap.add_argument("--" + name, required=True, type=Path)
    args = ap.parse_args()
    root = Path(tempfile.mkdtemp(prefix="sb-p05-", dir="/tmp")).resolve()
    os.chmod(root, 0o700)
    cell = ConnectionCell(
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
            f"Failed P05 cell retained at {root}; completed checks: {cell.checks}",
            flush=True,
        )
        raise
    finally:
        cell.close()


if __name__ == "__main__":
    main()
