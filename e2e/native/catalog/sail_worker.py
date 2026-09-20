"""One private worker per catalog principal; credentials enter over stdin only."""
import json
import os
import ipaddress
import sys

config = json.loads(sys.stdin.readline())
# TOML inline table encoding: JSON quoted strings are also valid TOML strings.
providers = ['{type="memory", name="spark_catalog", initial_database=["default"]}']
for catalog in ('p1','p2'):
    providers.append('{type="unity", name='+json.dumps(catalog)+', uri='+json.dumps(config['uri'])+
        ', database_cache_type="none", table_cache_type="none", view_cache_type="none", default_catalog='+json.dumps(catalog)+', token='+json.dumps(config['token'])+'}')
os.environ['SAIL_CATALOG__LIST'] = '['+','.join(providers)+']'
os.environ['SAIL_CATALOG__DEFAULT_CATALOG'] = 'spark_catalog'
for key, value in config.get('storage', {}).items():
    os.environ[key] = value
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession

server = SparkConnectServer(ip='127.0.0.1', port=0)
server.start()
import psutil
listeners = [c for c in psutil.Process().net_connections(kind='inet') if c.status == 'LISTEN']
assert listeners, 'Sail has no listening socket'
for connection in listeners:
    address = ipaddress.ip_address(connection.laddr.ip)
    assert (getattr(address,'ipv4_mapped',None) or address).is_loopback, connection.laddr
print('UC00 loopback listeners verified',file=sys.stderr,flush=True)
_, port = server.listening_address
spark = SparkSession.builder.remote(f'sc://127.0.0.1:{port}').getOrCreate()
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
