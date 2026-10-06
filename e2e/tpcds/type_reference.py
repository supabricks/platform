#!/usr/bin/env python3
"""Generate independently executed Apache Spark DATE/CHAR golden query results.

Requires pyspark==4.2.0 and Java 21. This uses JVM Spark, never Sail. Regenerate
only with reviewed type_cases.py, then compare the installed native results.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
from type_cases import cases

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def run(report):
    import pyspark
    import platform
    from pyspark.sql import SparkSession
    if report.exists():raise ValueError('fresh report required')
    with tempfile.TemporaryDirectory(prefix='eq171-reference-') as root:
        spark=(SparkSession.builder.master('local[2]').config('spark.ui.enabled','false')
            .config('spark.sql.warehouse.dir',root).config('spark.driver.memory','1g').getOrCreate())
        assert spark.version=='4.2.0' and spark.sparkContext.master=='local[2]'
        result=dict(engine='Apache Spark JVM',version=spark.version,fixture_sha256=sha(Path(__file__).with_name('type_cases.py')),kinds={})
        result['runtime']=dict(python=platform.python_version(),
            java=spark.sparkContext._jvm.java.lang.System.getProperty('java.runtime.version'),
            requirements_sha256=sha(Path(__file__).with_name('reference-requirements.txt')),
            generator_sha256=sha(Path(__file__)),
            jars={p.name:sha(p) for p in sorted((Path(pyspark.__file__).parent/'jars').glob('*.jar'))})
        assert result['runtime']['jars']
        try:
            spark.sql('CREATE DATABASE public').collect();spark.sql('USE public').collect()
            for kind in ('date','char'):
                spec=cases(kind);result['kinds'][kind]={}
                for phase,insert in [('bootstrap',spec['insert']),('updated',spec['updated'])]:
                    ddl=spec['ddl'].replace(' PRIMARY KEY','').replace(' text',' string')+' USING parquet'
                    spark.sql(ddl).collect();spark.sql(insert).collect()
                    rows=[]
                    for sql in spec['queries']:
                        frame=spark.sql(sql)
                        rows.append(dict(sql=sql,types=[f.dataType.simpleString() for f in frame.schema.fields],
                            rows=[[None if v is None else str(v) for v in row] for row in frame.collect()]))
                    result['kinds'][kind][phase]=rows;spark.sql('DROP TABLE typed').collect()
            result['status']='PASS';report.write_text(json.dumps(result,indent=2,ensure_ascii=True)+'\n')
        finally:spark.stop()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--report',type=Path,required=True);run(p.parse_args().report)
