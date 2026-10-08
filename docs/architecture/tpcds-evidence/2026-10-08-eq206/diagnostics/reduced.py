import hashlib,json,math,os,time
from pathlib import Path
from pyspark.sql import SparkSession
engine=os.environ.get('EQ206_ENGINE','sail');out=Path('/reports');out.mkdir(exist_ok=True)
server=None
if engine=='spark':spark=SparkSession.builder.master('local['+os.environ.get('EQ206_PARALLELISM','2')+']').config('spark.ui.enabled','false').config('spark.sql.shuffle.partitions','2').getOrCreate()
else:
 from pysail.spark import SparkConnectServer
 server=SparkConnectServer('127.0.0.1',0);server.start(background=True);spark=SparkSession.builder.remote('sc://127.0.0.1:'+str(server.listening_address[1])).getOrCreate()
cases={}
for name,values in dict(sf1_a='(10),(404),(13),(814)',sf1_b='(113),(838),(88),(105)',constant='(7),(7),(7),(7)',singleton='(7)',nulls='(1),(NULL),(2),(3)',all_nulls='(CAST(NULL AS DOUBLE)),(NULL)',nan="(CAST('NaN' AS DOUBLE)),(1.)",infinity="(CAST('Infinity' AS DOUBLE))",large_offset='(1000000000001.),(1000000000002.),(1000000000003.),(1000000000004.)',decimal='(CAST(0.0001 AS DECIMAL(18,4))),(0.0003),(0.0005)').items():
 cases[name]=f'SELECT var_samp(v) vs,var_pop(v) vp,stddev_samp(v) ss,stddev_pop(v) sp,avg(v) mean,stddev_samp(v)/avg(v) cov FROM VALUES {values} t(v)'
spark.sql('SELECT * FROM VALUES (10),(404),(13),(814) t(v)').coalesce(1).createOrReplaceTempView('single_partition')
cases['single_partition']='SELECT var_samp(v),stddev_samp(v),avg(v),stddev_samp(v)/avg(v) FROM single_partition'
cases['empty']='SELECT var_samp(v),var_pop(v),stddev_samp(v),stddev_pop(v) FROM VALUES (1.) t(v) WHERE false'
cases['distinct']='SELECT var_samp(DISTINCT v),var_pop(DISTINCT v),stddev_samp(DISTINCT v),stddev_pop(DISTINCT v) FROM VALUES (1),(2),(2),(3),(NULL) t(v)'
cases['grouped']='SELECT k,var_samp(v),stddev_samp(v),var_pop(v) FILTER (WHERE v<3) FROM VALUES (1,1),(1,2),(1,3),(2,8),(2,NULL) t(k,v) GROUP BY k ORDER BY k'
cases['aliases']='SELECT std(v),stddev(v),stddev_samp(v),variance(v),var_samp(v) FROM VALUES (1),(2),(3) t(v)'
cases['window']='SELECT id,var_samp(v) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) vs,stddev_samp(v) OVER (ORDER BY id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) ss FROM VALUES (1,10),(2,404),(3,13),(4,814),(5,NULL) t(id,v) ORDER BY id'
results=[]
try:
 for name,sql in cases.items():
  item=dict(id=name,sql=sql,sql_sha256=hashlib.sha256(sql.encode()).hexdigest());start=time.monotonic()
  try:
   frame=spark.sql(sql);item.update(status='complete',schema=frame.schema.jsonValue(),rows=[[None if v is None else str(v) for v in row] for row in frame.collect()])
  except Exception as error:item.update(status='failed',error=str(error))
  item['seconds']=time.monotonic()-start;results.append(item);print(name,item['status'],flush=True)
  (out/'result.json').write_text(json.dumps(dict(engine=engine,fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),queries=results),indent=2)+'\n')
finally:
 spark.stop()
 if server:server.stop()
