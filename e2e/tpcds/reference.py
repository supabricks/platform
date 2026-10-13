#!/usr/bin/env python3
"""Independent SF1 Apache Spark reference. Never counts as product coverage."""
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import time

from inputs import LOCK, inventory, sha, statements
from load import save, PROFILE


def run(args):
    import pyspark
    from pyspark.sql import SparkSession
    from pyspark.sql.types import StructType, StructField, StringType, IntegerType, DateType, DecimalType

    args.output.mkdir(parents=True, exist_ok=False)
    manifest = inventory(json.loads(LOCK.read_text()), args.inputs)
    generation = json.loads((args.dataset/'generation.json').read_text())
    profile=json.loads(PROFILE.read_text())
    assert generation['status']=='PASS' and generation['input_lock_sha256']==sha(LOCK)
    assert generation['business_rows']==19557335
    assert shutil.disk_usage(args.output).free >= 32*1024**3
    for t in generation['tables']:
        assert sha(args.dataset/'data'/t['file'])==t['sha256']
    result = dict(status='RUNNING', engine='Apache Spark JVM', version=pyspark.__version__,
                  scope='Independent SF1 reference only; not product results or an official TPC-DS score',
                  started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  input_lock_sha256=sha(LOCK), generation_receipt_sha256=sha(args.dataset/'generation.json'),
                  load_profile=profile,load_profile_sha256=sha(PROFILE),
                  fixture_sha256=sha(Path(__file__)), python=platform.python_version(),
                  bounds=dict(cpus=8, driver_memory='8g', query_timeout_seconds=120,
                              result_file_bytes=16*1024**2),
                  jars={p.name:sha(p) for p in sorted((Path(pyspark.__file__).parent/'jars').glob('*.jar'))},
                  tables=[], queries=[dict(q,status='not_run',reason='reference loading pending') for q in manifest['queries']])
    assert result['version']=='4.2.0' and result['jars']
    save(args.output/'result.json',result)
    started=time.monotonic(); spark=None
    try:
        spark=(SparkSession.builder.master('local[8]').config('spark.ui.enabled','false')
               .config('spark.driver.memory','8g').config('spark.sql.shuffle.partitions','32')
               .config('spark.sql.session.timeZone','UTC')
               .config('spark.sql.warehouse.dir',str(args.output/'warehouse'))
               .config('spark.local.dir',str(args.output/'spill')).getOrCreate())
        result['java']=spark.sparkContext._jvm.java.lang.System.getProperty('java.runtime.version')
        result['spark_configuration']=dict(spark.sparkContext.getConf().getAll())
        spark.sql('CREATE DATABASE public').collect();spark.sql('USE public').collect()
        for table in manifest['tables']:
            start=time.monotonic(); fields=[]
            for c in table['columns']:
                kind=c['type']
                if kind=='integer': typ=IntegerType()
                elif kind=='date': typ=DateType()
                elif kind.startswith('decimal('): typ=DecimalType(*map(int,kind[8:-1].split(',')))
                else: typ=StringType()
                fields.append(StructField(c['name'],typ,True))
            # A declared final empty field consumes the generator's trailing '|'.
            # FAILFAST and the preflight hashes prevent dropping malformed rows.
            fields.append(StructField('_terminator',StringType(),True))
            frame=(spark.read.schema(StructType(fields)).option('sep','|').option('quote','')
                   .option('encoding',profile['input_encoding'])
                   .option('nullValue','').option('mode','FAILFAST')
                   .csv(str(args.dataset/'data'/(table['name']+'.dat'))).drop('_terminator'))
            columns=', '.join(c['name']+' '+c['type'] for c in table['columns'])
            # Spark's real CHAR(n) table writer enforces padding; do not trim it
            # or substitute a string view with different comparison semantics.
            spark.sql('CREATE TABLE '+table['name']+' ('+columns+') USING parquet').collect()
            frame.write.insertInto(table['name'])
            rows=spark.table(table['name']).count()
            expected=next(t['rows'] for t in generation['tables'] if t['file']==table['name']+'.dat')
            assert rows==expected,table['name']
            result['tables'].append(dict(name=table['name'],rows=rows,load_seconds=time.monotonic()-start,
                                         schema=spark.table(table['name']).schema.jsonValue()))
            save(args.output/'result.json',result);print('REFERENCE LOADED',table['name'],rows,flush=True)
        result['load_seconds']=time.monotonic()-started
        query_root=args.output/'queries';query_root.mkdir()
        executor=concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            for entry in result['queries']:
                identifier=entry['id']; sql=statements((args.inputs/'spark'/(identifier+'.sql')).read_text())[0]
                def execute():
                    # Set the job group on the executing thread so cancellation
                    # addresses every Spark action for this statement.
                    spark.sparkContext.setJobGroup(identifier,identifier,interruptOnCancel=True)
                    frame=spark.sql(sql);output=query_root/(identifier+'.rows.jsonl'); count=size=0
                    schema=frame.schema.jsonValue()
                    (query_root/(identifier+'.plan.txt')).write_text(frame._jdf.queryExecution().toString())
                    with output.open('x') as stream:
                        for row in frame.toLocalIterator():
                            line=json.dumps([None if v is None else str(v) for v in row],ensure_ascii=True)+'\n'
                            size+=len(line.encode())
                            if size>16*1024**2: raise ValueError('full result exceeds declared evidence ceiling')
                            stream.write(line);count+=1
                    return dict(rows=count,schema=schema,result_sha256=sha(output),result_bytes=size)
                start=time.monotonic();future=executor.submit(execute)
                entry.pop('reason',None)
                try:
                    entry.update(future.result(timeout=120),status='complete')
                except concurrent.futures.TimeoutError:
                    entry.update(status='timeout',reason='120-second statement ceiling')
                    spark.sparkContext.cancelJobGroup(identifier)
                    try:future.result(timeout=15)
                    except concurrent.futures.TimeoutError:
                        raise RuntimeError('reference cancellation did not finish; stop remaining suite')
                    except Exception:pass
                except Exception as error:
                    entry.update(status='failed',reason=str(error))
                finally:
                    entry['elapsed_seconds']=time.monotonic()-start
                    save(args.output/'result.json',result)
                    print('REFERENCE QUERY',identifier,entry['status'],round(entry['elapsed_seconds'],3),flush=True)
        finally:
            executor.shutdown(wait=False,cancel_futures=True)
        result['completed_queries']=sum(q['status']=='complete' for q in result['queries'])
        result['status']='PASS' if result['completed_queries']==103 else 'INCOMPLETE'
    except BaseException as error:
        result.update(status='FAIL',error=str(error));raise
    finally:
        result['elapsed_seconds']=time.monotonic()-started
        save(args.output/'result.json',result)
        if spark is not None:spark.stop()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('inputs','dataset','output'):parser.add_argument('--'+name,type=Path,required=True)
    run(parser.parse_args())
