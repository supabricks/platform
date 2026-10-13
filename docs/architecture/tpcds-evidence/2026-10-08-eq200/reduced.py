import argparse, hashlib, json, time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--engine',choices=['sail','spark'],required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
assert not args.output.exists()
from pyspark.sql import SparkSession
server=None
if args.engine=='sail':
    from pysail.spark import SparkConnectServer
    server=SparkConnectServer('127.0.0.1',0);server.start(background=True)
    spark=SparkSession.builder.remote('sc://127.0.0.1:'+str(server.listening_address[1])).getOrCreate()
else:
    spark=(SparkSession.builder.master('local[2]').config('spark.ui.enabled','false')
           .config('spark.driver.memory','1g').config('spark.sql.warehouse.dir','/reports/warehouse').getOrCreate())
cases=[]
for sign in [1,-1]:
    for numerator,count in [(2,3),(1,32),(1,3)]:
        sql=f"SELECT avg(CAST(CASE WHEN id=0 THEN {sign*numerator}/100.0 ELSE 0 END AS DECIMAL(18,2))) a FROM range({count})"
        cases.append((f'sign{sign}_sum{numerator}_count{count}',sql))
for function in ['avg','mean']:
    cases += [(function+'_distinct',f'SELECT {function}(DISTINCT CAST(v AS DECIMAL(18,2))) a FROM VALUES (0.01),(0.01),(0.00),(0.04),(NULL) t(v)'),
        (function+'_grouped',f'SELECT s, {function}(CAST(s*CASE WHEN id=0 THEN 0.02 ELSE 0 END AS DECIMAL(18,2))) a FROM range(3) CROSS JOIN VALUES (1),(-1) t(s) GROUP BY s ORDER BY s'),
        (function+'_window',f'SELECT id, {function}(CAST(CASE WHEN id=0 THEN 0.01 ELSE 0 END AS DECIMAL(18,2))) OVER () a FROM range(32) ORDER BY id'),
        (function+'_sliding',f'SELECT id, {function}(CAST(CASE WHEN id % 3=0 THEN -0.02 ELSE 0 END AS DECIMAL(18,2))) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) a FROM range(7) ORDER BY id')]
cases += [('all_null','SELECT avg(CAST(NULL AS DECIMAL(18,2))) a FROM range(3)'),('empty','SELECT avg(CAST(id AS DECIMAL(18,2))) a FROM range(0)'),('float','SELECT avg(CAST(id AS DOUBLE)) a FROM range(3)')]
result={'engine':args.engine,'fixture_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'queries':[]}
if args.engine=='spark':result['java']=spark.sparkContext._jvm.java.lang.System.getProperty('java.runtime.version')
try:
    for name,sql in cases:
        start=time.monotonic();entry={'id':name,'sql':sql}
        try:
            frame=spark.sql(sql)
            entry.update(schema=frame.schema.jsonValue(),rows=[[None if v is None else str(v) for v in row] for row in frame.collect()],status='complete')
        except Exception as error:entry.update(status='failed',error=str(error))
        entry['elapsed_seconds']=time.monotonic()-start;result['queries'].append(entry)
        args.output.write_text(json.dumps(result,indent=2)+'\n');print(name,entry['status'],flush=True)
finally:
    spark.stop()
    if server:server.stop()
