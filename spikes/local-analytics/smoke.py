"""Check Sail/Delta interoperability; no Postgres or Supabricks runtime involved."""

import argparse
import importlib.metadata
import json
import platform
import tempfile
import time
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import psutil
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession
from pyspark.errors import IllegalArgumentException, UnsupportedOperationException

ANALYTICS = Path(__file__).resolve().parents[2] / "python/analytics"
sys.path.insert(0, str(ANALYTICS))
from read_delta import local_rows


def check(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def read_rows(spark, path, version=None):
    reader = spark.read.format("delta")
    if version is not None:
        reader = reader.option("versionAsOf", str(version))
    return [(r.id, r.amount) for r in reader.load(path).orderBy("id").collect()]


def run(report, launch_time):
    report.update({
        "scope": "synthetic Delta interoperability; not an OLTAP or durability test",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "versions": {
            p: importlib.metadata.version(p)
            for p in ("pysail", "pyspark-client", "deltalake", "pyarrow", "pandas")
        },
        "checks": [],
    })
    schema = pa.schema(
        [("id", pa.int64()), ("region", pa.string()), ("amount", pa.decimal128(18, 2))]
    )
    original = [(1, Decimal("100.00")), (2, Decimal("150.00"))]
    updated = [(1, Decimal("125.00")), (3, Decimal("200.00"))]

    with tempfile.TemporaryDirectory(prefix="supabricks-analytics-smoke-") as tmp:
        path = str(Path(tmp) / "orders")
        write_deltalake(
            path,
            pa.Table.from_pylist(
                [
                    {"id": 1, "region": "west", "amount": Decimal("100.00")},
                    {"id": 2, "region": "east", "amount": Decimal("150.00")},
                ],
                schema=schema,
            ),
        )

        server = SparkConnectServer()
        spark = None
        started = time.perf_counter()
        server.start()
        try:
            _, port = server.listening_address
            spark = SparkSession.builder.remote(f"sc://localhost:{port}").getOrCreate()
            check(spark.sql("SELECT 1 + 1 AS n").first().n, 2, "Spark SQL")
            report["server_start_to_first_query_seconds_excluding_imports"] = round(
                time.perf_counter() - started, 3
            )
            report["process_launch_to_first_query_seconds_including_imports"] = round(
                time.perf_counter() - launch_time, 3
            )
            report["checks"].append("Spark Connect SQL")

            totals = (
                spark.read.format("delta")
                .load(path)
                .groupBy("region")
                .sum("amount")
                .orderBy("region")
                .collect()
            )
            check(
                [(r[0], r[1]) for r in totals],
                [("east", Decimal("150.00")), ("west", Decimal("100.00"))],
                "DataFrame decimal aggregate",
            )
            report["checks"].append("DataFrame aggregation preserves decimal values")

            # Values above double's exact integer range and fractional cents expose
            # silent decimal -> float conversion; include null and negative values.
            exact_path = str(Path(tmp) / "exact")
            exact_schema = pa.schema([("id", pa.int64()), ("amount", pa.decimal128(28, 4))])
            exact_values = [Decimal("9007199254740993.0001"), Decimal("-0.0002"), None]
            write_deltalake(exact_path, pa.Table.from_pylist(
                [{"id": i, "amount": amount} for i, amount in enumerate(exact_values)],
                schema=exact_schema))
            exact = spark.read.format("delta").load(exact_path)
            check([r.amount for r in exact.orderBy("id").collect()], exact_values,
                  "large, negative and null decimals")
            check(exact.groupBy().sum("amount").first()[0],
                  Decimal("9007199254740992.9999"), "exact fractional aggregation")
            empty_path = str(Path(tmp) / "empty")
            write_deltalake(empty_path, pa.Table.from_pylist([], schema=exact_schema))
            empty = spark.read.format("delta").load(empty_path)
            check(empty.count(), 0, "empty Delta table")
            check(empty.schema["amount"].dataType.simpleString(), "decimal(28,4)",
                  "empty table retains decimal schema")
            report["checks"].append("large/negative/null decimals and empty table schema")

            table = DeltaTable(path)
            table.update(updates={"amount": "125.00"}, predicate="id = 1")
            table.delete(predicate="id = 2")
            write_deltalake(
                path,
                pa.Table.from_pylist(
                    [{"id": 3, "region": "north", "amount": Decimal("200.00")}],
                    schema=schema,
                ),
                mode="append",
            )
            check(read_rows(spark, path), updated, "Sail reads delta-rs mutations")
            check(read_rows(spark, path, 0), original, "historical DataFrame read")
            report["checks"].extend(
                ["delta-rs update/delete/insert visible in Sail", "historical DataFrame read"]
            )

            # Use a named table: embedding a path relation in a persisted view
            # failed on readback in the initial exploratory probe.
            spark.sql("CREATE DATABASE source").collect()
            spark.sql("CREATE DATABASE app").collect()
            # Spark string literals accept backslash escapes as well as quotes.
            sql_path = path.replace("\\", "\\\\").replace("'", "\\'")
            spark.sql(
                f"CREATE TABLE source.orders USING delta LOCATION '{sql_path}'"
            ).collect()
            spark.sql(
                "CREATE VIEW app.orders AS "
                "SELECT * FROM source.orders VERSION AS OF 0"
            ).collect()
            check(
                [(r.id, r.amount) for r in spark.table("app.orders").orderBy("id").collect()],
                original,
                "named catalog view pins the historical version",
            )
            report["checks"].append("spark.table name bound through VERSION AS OF view")

            # Probe the known unsafe form explicitly: accepting SQL is not proof
            # that a version option was honored. This must remain out of A03.
            spark.sql(
                "CREATE TABLE source.option_orders USING delta "
                f"OPTIONS (versionAsOf '0') LOCATION '{sql_path}'"
            ).collect()
            check([(r.id, r.amount) for r in spark.table("source.option_orders").orderBy("id").collect()],
                  updated, "known unsafe versionAsOf table option reads latest")
            report["checks"].append("unsafe table option regression: reads latest, not version 0")
            spark.sql(
                "CREATE VIEW app.path_orders AS "
                f"SELECT * FROM delta.`{path.replace('`', '``')}` VERSION AS OF 0"
            ).collect()
            try:
                spark.table("app.path_orders").collect()
            except IllegalArgumentException as error:
                if "found /" not in str(error) or "expected identifier" not in str(error):
                    raise
                report["unsafe_path_view_error"] = str(error)
            else:
                raise AssertionError("direct-path view behavior changed; requalify snapshot binding")
            report["checks"].append("unsafe direct-path view regression: creation succeeds, read fails")

            # Mutate after binding, and query both epochs in the same engine.
            write_deltalake(path, pa.Table.from_pylist(
                [{"id": 4, "region": "west", "amount": Decimal("0.01")}], schema=schema),
                mode="append")
            updated = [*updated, (4, Decimal("0.01"))]
            latest_version = DeltaTable(path).version()
            spark.sql(
                "CREATE VIEW app.current_orders AS "
                f"SELECT * FROM source.orders VERSION AS OF {latest_version}"
            ).collect()
            check([(r.id, r.amount) for r in spark.table("app.orders").orderBy("id").collect()],
                  original, "existing named view stays pinned after append")
            check([(r.id, r.amount) for r in spark.table("app.current_orders").orderBy("id").collect()],
                  updated, "second named view reads a newer pinned version")
            report["checks"].append("two named views retain distinct epochs after mutation")

            # Independently reopen through the writer library. This checks table
            # interoperability, not independent database-wide snapshot publication.
            check(
                sorted((r["id"], r["amount"]) for r in local_rows(path)),
                updated,
                "delta-rs independent table reader",
            )
            report["checks"].append("independent Delta reader agrees")
            independent = subprocess.check_output([
                sys.executable, "-W", "error", str(ANALYTICS / "read_delta.py"), path], text=True)
            check(json.loads(independent), [[i, str(amount)] for i, amount in updated],
                  "fresh delta-rs process reads Sail-visible values")
            report["checks"].append("separate delta-rs process agrees on current rows")

            # Exercise the reverse direction on a separately owned synthetic table.
            # This does not authorize writes into Postgres-owned export generations.
            sail_path = str(Path(tmp) / "sail_written")
            spark.read.format("delta").load(path).write.format("delta").save(sail_path)
            check(sorted((r["id"], r["amount"]) for r in
                         local_rows(sail_path)),
                  updated, "delta-rs reads Sail DataFrame write")
            sail_sql_path = sail_path.replace("\\", "\\\\").replace("'", "\\'")
            spark.sql(f"CREATE TABLE source.sail_written USING delta LOCATION '{sail_sql_path}'").collect()
            report["unsupported_sail_commands"] = {}
            for sql, command in (
                ("UPDATE source.sail_written SET amount = 126.00 WHERE id = 1", "Update"),
            ):
                try:
                    spark.sql(sql).collect()
                except UnsupportedOperationException as error:
                    check(str(error), f"CommandNode::{command}", f"unsupported Sail {command}")
                    report["unsupported_sail_commands"][command] = str(error)
                else:
                    raise AssertionError(f"Sail {command} support changed; requalify mutations")
            check(sorted((r["id"], r["amount"]) for r in local_rows(sail_path)), updated,
                  "rejected UPDATE leaves data unchanged")
            spark.sql("DELETE FROM source.sail_written WHERE id = 3").collect()
            check(sorted((r["id"], r["amount"]) for r in local_rows(sail_path)),
                  [(1, Decimal("125.00")), (4, Decimal("0.01"))], "delta-rs reads Sail DELETE")
            sail_table = DeltaTable(sail_path)
            sail_table.update(updates={"amount": "126.00"}, predicate="id = 1")
            sail_table.delete(predicate="id = 4")
            check(sorted((r["id"], r["amount"]) for r in
                         local_rows(sail_path)),
                  [(1, Decimal("126.00"))],
                  "delta-rs mutates a Sail-written table")
            check(read_rows(spark, sail_path), [(1, Decimal("126.00"))],
                  "Sail reads delta-rs mutations of its own write")
            report["checks"].append("Sail writes/deletes readable and mutable by delta-rs, reread by Sail")
            report["checks"].append("Sail SQL UPDATE explicitly unsupported and leaves data unchanged")
            children = psutil.Process().children(recursive=True)
            child_names = [p.name() for p in children]
            if any("java" in name.lower() for name in child_names):
                raise AssertionError(f"Unexpected Java child: {child_names}")
            report["child_processes_at_measurement"] = child_names
            report["checks"].append("no Java child process at measurement")
            report["combined_python_client_server_writer_rss_mib"] = round(
                psutil.Process().memory_info().rss / 1024**2, 1
            )
        finally:
            try:
                if spark is not None:
                    spark.stop()
            finally:
                server.stop()

        # Start a fresh engine against existing files and rebuild its catalog.
        # This verifies engine restart, not machine/power-loss durability.
        server = SparkConnectServer()
        spark = None
        server.start()
        try:
            _, port = server.listening_address
            spark = SparkSession.builder.remote(f"sc://localhost:{port}").getOrCreate()
            check(read_rows(spark, path), updated, "fresh engine reads existing table")
            check(read_rows(spark, path, 0), original, "fresh engine reads history")
            spark.sql("CREATE DATABASE source").collect()
            spark.sql("CREATE DATABASE app").collect()
            spark.sql(f"CREATE TABLE source.orders USING delta LOCATION '{sql_path}'").collect()
            spark.sql("CREATE VIEW app.orders AS SELECT * FROM source.orders VERSION AS OF 0").collect()
            check([(r.id, r.amount) for r in spark.table("app.orders").orderBy("id").collect()],
                  original, "fresh engine rebuilds pinned named view")
            report["checks"].append("fresh Sail engine reads current and historical versions")
        finally:
            try:
                if spark is not None:
                    spark.stop()
            finally:
                server.stop()

    report["status"] = "PASS"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--launch-time", type=float, default=time.perf_counter())
    args = parser.parse_args()
    report = {"status": "FAIL"}
    try:
        run(report, args.launch_time)
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        rendered = json.dumps(report, indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered)
        else:
            print(rendered)


if __name__ == "__main__":
    main()
