"""Private UC capability check on host; NOT a governed workload or isolation proof."""
import json
import os
import sys
config=json.loads(sys.stdin.readline())
os.environ['SAIL_CATALOG__LIST']='[{type="unity", name="governed", uri='+json.dumps(config['uri'])+', default_catalog="alice", token='+json.dumps(config['token'])+', database_cache_type="none", table_cache_type="none", view_cache_type="none"}]'
os.environ['SAIL_CATALOG__DEFAULT_CATALOG']='governed'
os.environ.update(TZ='UTC',TOKIO_WORKER_THREADS='2',RAYON_NUM_THREADS='2',OPENBLAS_NUM_THREADS='1')
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession
server=SparkConnectServer(ip='127.0.0.1',port=0); server.start()
_,port=server.listening_address
spark=SparkSession.builder.remote(f'sc://127.0.0.1:{port}').getOrCreate()
try:
    assert spark.sql('SELECT 1 AS positive').first()['positive']==1
    try:
        rows=[r.asDict() for r in spark.sql('SELECT id FROM governed.data.private').collect()]
        result=dict(ok=True,rows=rows)
    except Exception as error:
        result=dict(ok=False,error_type=type(error).__name__)
    print('IAM00_RESULT '+json.dumps(result),flush=True)
finally:
    spark.stop(); server.stop()
