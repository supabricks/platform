import argparse, hashlib, json, os, resource, subprocess, sys, threading, time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--query');a=p.parse_args()
profiles={'compact':dict(pool_mib=256,spill_mib=256,rss_mib=1024,file_mib=16), 'medium':dict(pool_mib=1024,spill_mib=4096,rss_mib=4096,file_mib=1024), 'large':dict(pool_mib=2048,spill_mib=8192,rss_mib=6144,file_mib=1024)}
profile=profiles[a.profile];out=Path('/reports')/a.profile
queries='q21 q39a q39b q47 q66 q67 q72 q75 q78'.split()
if not a.query:
    import psutil
    out.mkdir(exist_ok=False)
    (out/'profile.json').write_text(json.dumps(dict(profile=profile,cpus='0-7',container_memory_mib=16384,swap=False,source='528b49dac7beafa6d5a0e1f2e538efcbbc4aca2f',fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()),indent=2))
    for q in queries:
        d=out/q;d.mkdir();(d/'spill').mkdir()
        start=time.monotonic();peak=spill=maxfile=0;reason=None
        with (d/'worker.log').open('w') as log:
            child=subprocess.Popen([sys.executable,__file__,'--profile',a.profile,'--query',q],stdout=log,stderr=subprocess.STDOUT)
            process=psutil.Process(child.pid)
            while child.poll() is None:
                try:peak=max(peak,process.memory_info().rss)
                except psutil.NoSuchProcess:pass
                sizes=[]
                for f in (d/'spill').rglob('*'):
                    try:
                        if f.is_file():sizes.append(f.stat().st_size)
                    except FileNotFoundError:pass
                spill=max(spill,sum(sizes));maxfile=max([maxfile]+sizes)
                if peak>profile['rss_mib']*1024**2 or time.monotonic()-start>180:
                    reason='rss_ceiling' if peak>profile['rss_mib']*1024**2 else 'timeout';child.kill();child.wait();break
                time.sleep(.1)
        metrics=dict(exit_code=child.returncode,reason=reason,peak_rss_bytes=peak,peak_spill_bytes=spill,peak_spill_file_bytes=maxfile,process_seconds=time.monotonic()-start)
        (d/'metrics.json').write_text(json.dumps(metrics,indent=2));print(q,metrics,flush=True)
    sys.exit()
d=out/a.query
os.environ['SAIL_CATALOG__LIST']='[{type="memory", name="spark_catalog", initial_database=["default"]}]'
os.environ['SAIL_CATALOG__DEFAULT_CATALOG']='spark_catalog'
os.environ.update(SAIL_MODE='local',SAIL_RUNTIME__MEMORY_POOL__TYPE='fair',SAIL_RUNTIME__MEMORY_POOL__FAIR__MAX_SIZE=str(profile['pool_mib']*1024**2),SAIL_RUNTIME__TEMPORARY_FILES__MAX_SIZE=str(profile['spill_mib']*1024**2),SAIL_RUNTIME__TEMPORARY_FILES__PATHS=json.dumps([str(d/'spill')]),TOKIO_WORKER_THREADS='2',RAYON_NUM_THREADS='2',OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',RUST_LOG='error',TZ='UTC')
time.tzset();resource.setrlimit(resource.RLIMIT_CORE,(0,0));resource.setrlimit(resource.RLIMIT_FSIZE,(profile['file_mib']*1024**2,)*2)
from pysail.spark import SparkConnectServer
from pyspark.sql import SparkSession
server=SparkConnectServer('127.0.0.1',0);server.start(background=True)
spark=SparkSession.builder.remote('sc://127.0.0.1:'+str(server.listening_address[1])).getOrCreate()
spark.conf.set('spark.sql.session.timeZone','UTC')
loaded=json.loads(Path('/load/result.json').read_text());desc=loaded['publication']['descriptor']
spark.sql('CREATE DATABASE _supabricks_source').collect();spark.sql('CREATE DATABASE public').collect()
for table in desc['manifest']['tables']:
    source=f"spark_catalog._supabricks_source.t_{table['oid']}";path=Path('/load/state')/desc['generation']/table['path']
    spark.sql(f"CREATE TABLE {source} USING delta LOCATION '{path}'").collect()
    spark.sql(f"CREATE VIEW public.`{table['name']}` AS SELECT * FROM {source} VERSION AS OF {int(table['version'])}").collect()
spark.sql('USE DATABASE public').collect()
sql=(Path('/inputs')/(a.query+'.sql')).read_text();result=dict(id=a.query,sql_sha256=hashlib.sha256(sql.encode()).hexdigest())
try:
    plan=spark.sql('EXPLAIN FORMATTED '+sql).collect();(d/'plan.txt').write_text('\n'.join(str(row[0]) for row in plan))
    start=time.monotonic();frame=spark.sql(sql);result['schema']=frame.schema.jsonValue();count=0
    with (d/'rows.jsonl').open('x') as stream:
        for row in frame.toLocalIterator():stream.write(json.dumps([None if v is None else str(v) for v in row],ensure_ascii=True,separators=(',',':'))+'\n');count+=1
    result.update(status='complete',rows=count,elapsed_seconds=time.monotonic()-start)
except Exception as error:result.update(status='failed',error=str(error),elapsed_seconds=time.monotonic()-start if 'start' in globals() else None)
(d/'result.json').write_text(json.dumps(result,indent=2));print(result['status'],flush=True)
spark.stop();server.stop()
