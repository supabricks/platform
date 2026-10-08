import argparse,hashlib,json,os,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
os.environ.update(SAIL_CATALOG__LIST='[{type="memory", name="spark_catalog", initial_database=["default"]}]',SAIL_CATALOG__DEFAULT_CATALOG='spark_catalog',RUST_LOG='datafusion_optimizer=debug,datafusion=debug',TOKIO_WORKER_THREADS='2',RAYON_NUM_THREADS='2')
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession
import pyarrow as pa
import pyarrow.parquet as pq
from decimal import Decimal
server=SparkConnectServer('127.0.0.1',0);server.start(background=True);spark=SparkSession.builder.remote('sc://127.0.0.1:'+str(server.listening_address[1])).getOrCreate()
root=a.output.parent;root.mkdir(exist_ok=True,parents=True);results=[]
for char in [True]:
 name='items_char' if char else 'items_string';table=root/name;table.mkdir()
 fields=[dict(name='id',type='integer',nullable=True,metadata={}),dict(name='c',type='string',nullable=True,metadata={'__CHAR_VARCHAR_TYPE_STRING':'char(4)'} if char else {}),dict(name='v',type='decimal(7,2)',nullable=True,metadata={})]
 schema=pa.schema([pa.field('id',pa.int32()),pa.field('c',pa.string()),pa.field('v',pa.decimal128(7,2))]);rows=[dict(id=i,c=c,v=Decimal(v)) for i,c,v in [(1,'x   ','1'),(2,'x   ','3'),(3,'y   ','5'),(4,None,'7')]]
 pq.write_table(pa.Table.from_pylist(rows,schema=schema),table/'data.parquet');log=table/'_delta_log';log.mkdir()
 actions=[dict(protocol=dict(minReaderVersion=1,minWriterVersion=2)),dict(metaData=dict(id=name,format=dict(provider='parquet',options={}),schemaString=json.dumps(dict(type='struct',fields=fields)),partitionColumns=[],configuration={})),dict(add=dict(path='data.parquet',size=(table/'data.parquet').stat().st_size,modificationTime=0,partitionValues={},dataChange=True))]
 (log/'00000000000000000000.json').write_text('\n'.join(map(json.dumps,actions))+'\n')
 spark.sql(f"CREATE TABLE src_{name} USING delta LOCATION '{table}'").collect();spark.sql(f'CREATE VIEW {name} AS SELECT * FROM src_{name} VERSION AS OF 0').collect()
 for mode,source in [('table','src_'+name)]:
  cases=[('correlated_avg',f'SELECT a.id FROM {source} a WHERE a.v > (SELECT avg(b.v) FROM {source} b WHERE CAST(b.c AS STRING)=CAST(a.c AS STRING)) ORDER BY a.id'),('correlated_count',f'SELECT a.id FROM {source} a WHERE (SELECT count(*) FROM {source} b WHERE b.c=a.c)>0 ORDER BY a.id'),('exists',f'SELECT a.id FROM {source} a WHERE EXISTS (SELECT 1 FROM {source} b WHERE CAST(b.c AS STRING)=CAST(a.c AS STRING)) ORDER BY a.id'),('uncorrelated',f"SELECT a.id FROM {source} a WHERE a.c='x' ORDER BY a.id")]
  for kind,sql in cases[:1]:
   entry=dict(id=f'{name}_{mode}_{kind}',sql=sql);start=time.monotonic()
   try:
    frame=spark.sql(sql);entry.update(status='complete',schema=frame.schema.jsonValue(),rows=[[None if v is None else str(v) for v in row] for row in frame.collect()])
   except Exception as error:entry.update(status='failed',error=str(error))
   entry['elapsed_seconds']=time.monotonic()-start;results.append(entry);print(entry['id'],entry['status'],entry.get('error','')[:180],flush=True)
   a.output.write_text(json.dumps(dict(fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),queries=results),indent=2)+'\n')
spark.stop();server.stop()
