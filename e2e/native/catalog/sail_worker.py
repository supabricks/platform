"""One private worker per catalog principal; credentials enter over stdin only."""
import json
import os
import sys

config = json.loads(sys.stdin.readline())
# TOML inline table encoding: JSON quoted strings are also valid TOML strings.
os.environ['SAIL_CATALOG__LIST'] = '[{type="memory", name="spark_catalog", initial_database=["default"]}, {type="unity", name="uc", uri='+json.dumps(config['uri'])+', default_catalog="p1", token='+json.dumps(config['token'])+'}]'
os.environ['SAIL_CATALOG__DEFAULT_CATALOG'] = 'spark_catalog'
for key, value in config.get('storage', {}).items():
    os.environ[key] = value
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession

server = SparkConnectServer()
server.start()
_, port = server.listening_address
spark = SparkSession.builder.remote(f'sc://localhost:{port}').getOrCreate()
try:
    for line in sys.stdin:
        request = json.loads(line)
        try:
            result = dict(ok=True, rows=[r.asDict(recursive=True) for r in spark.sql(request['sql']).collect()])
        except Exception as error:
            # Raw messages may contain URLs/credentials; keep them in the private
            # diagnostic log, never in the public capability report.
            print(str(error), file=sys.stderr, flush=True)
            result = dict(ok=False, error_type=type(error).__name__)
        print('UC00_RESULT '+json.dumps(result, default=str), flush=True)
finally:
    spark.stop()
    server.stop()
