"""Check Sail/Delta interoperability; no Postgres or Supabricks runtime involved."""

import importlib.metadata
import json
import platform
import tempfile
import time
from decimal import Decimal
from pathlib import Path

import psutil
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession


def check(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: {actual!r} != {expected!r}")


def read_rows(spark, path, version=None):
    reader = spark.read.format("delta")
    if version is not None:
        reader = reader.option("versionAsOf", str(version))
    return [(r.id, r.amount) for r in reader.load(path).orderBy("id").collect()]


def main():
    report = {
        "scope": "synthetic Delta interoperability; not an OLTAP or durability test",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "versions": {
            p: importlib.metadata.version(p)
            for p in ("pysail", "pyspark-client", "deltalake", "pyarrow", "pandas")
        },
        "checks": [],
    }
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

            # Independently reopen through the writer library. This checks table
            # interoperability, not independent database-wide snapshot publication.
            check(
                sorted((r["id"], r["amount"]) for r in DeltaTable(path).to_pyarrow_table().to_pylist()),
                updated,
                "delta-rs independent table reader",
            )
            report["checks"].append("independent Delta reader agrees")
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
            report["checks"].append("fresh Sail engine reads current and historical versions")
        finally:
            try:
                if spark is not None:
                    spark.stop()
            finally:
                server.stop()

    report["status"] = "PASS"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
