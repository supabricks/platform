"""One source-built Sail instance per lease; no catalog/admin credential inside."""
from pathlib import Path
import time
import os
os.environ.update(SAIL_MODE="local", SAIL_RUNTIME__MEMORY_POOL__TYPE="fair",
    SAIL_RUNTIME__MEMORY_POOL__FAIR__MAX_SIZE="268435456",
    SAIL_RUNTIME__TEMPORARY_FILES__MAX_SIZE="268435456",
    SAIL_RUNTIME__TEMPORARY_FILES__PATHS='["/scratch/spill"]',
    SAIL_CATALOG__LIST='[{type="memory",name="spark_catalog",initial_database=["default"]}]',
    SAIL_CATALOG__DEFAULT_CATALOG="spark_catalog")
Path("/scratch/spill").mkdir(exist_ok=True)
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession
server = SparkConnectServer(ip='127.0.0.1', port=15002)
server.start()
spark = SparkSession.builder.remote('sc://127.0.0.1:15002').getOrCreate()
spark.client.set_retry_policies([])
spark.sql('SELECT 1').collect()
spark.sql('CREATE DATABASE _supabricks').collect()
spark.sql("CREATE VIEW _supabricks.epoch AS SELECT 'iam00' AS epoch_id")
Path('/scratch/sail-ready').write_text(spark.session_id)
try:
    while True: time.sleep(1)
finally:
    spark.stop(); server.stop()
