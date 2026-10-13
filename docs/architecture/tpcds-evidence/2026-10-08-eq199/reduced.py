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
for p1,s1,p2,s2 in [(15,4,15,4),(17,2,17,2),(21,2,27,2),(21,4,17,2),(38,18,38,18),(38,0,38,0),(38,38,38,38),(7,2,7,2)]:
    for op in ['+','-','*','/']:
        cases.append((f'{p1}_{s1}_{op}_{p2}_{s2}',f"SELECT CAST(id+1 AS DECIMAL({p1},{s1})) {op} CAST(3 AS DECIMAL({p2},{s2})) a FROM range(2) ORDER BY id"))
for expression in ['v*100','v/3','v/3.0','v/CAST(3 AS BIGINT)','CAST(3 AS BIGINT)/v','v*2','v+1','v-1','v+1.0D','round(v/v,2)','v/(v+v+v)/3*100']:
    cases.append((expression,f'SELECT {expression} a FROM (SELECT CAST(id+1 AS DECIMAL(17,2)) v FROM range(2)) ORDER BY v'))
cases.extend([
 ('nested_sum','SELECT sum(v)*100/sum(sum(v)) OVER () a FROM (SELECT id%2 g,CAST(id+1 AS DECIMAL(7,2)) v FROM range(5)) GROUP BY g ORDER BY g'),
 ('round_sum','SELECT round(sum(v)/sum(v+1),2) a FROM (SELECT CAST(id+1 AS DECIMAL(7,2)) v FROM range(5))'),
 ('int_sum_decimal','SELECT sum(id)/3.0 a FROM range(5)'),
 ('negative_division','SELECT v,CAST(v AS DECIMAL(15,4))/CAST(32 AS DECIMAL(15,4)) a FROM VALUES (-1),(1),(NULL) t(v) ORDER BY v'),
])
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
