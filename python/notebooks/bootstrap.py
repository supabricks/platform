"""Fixed, pre-execution Spark bootstrap; never load startup code from a notebook."""
import json
import os
import sys
import site
from pathlib import Path
try:
    context = json.loads(Path(os.environ['SUPABRICKS_NOTEBOOK_CONTEXT']).read_text())
    if sys.prefix != context['python_prefix'] or sys.prefix == sys.base_prefix or site.ENABLE_USER_SITE:
        raise RuntimeError('kernel environment prefix differs')
    supabricks_environment = context['environment']
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.remote(context['endpoint']).getOrCreate()
    epoch = spark.sql('SELECT * FROM _supabricks.epoch').first().asDict()
    if epoch['epoch_id'] != context['epoch_id']:
        raise RuntimeError('epoch binding differs')
    target = Path(context['ready'])
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps({'epoch_id': epoch['epoch_id'], 'environment': supabricks_environment}))
    temporary.replace(target)
    del context, target, temporary
except BaseException:
    # IPython normally logs exec_files errors and continues. A failed bootstrap
    # must never admit user execution with an unbound or missing Spark context.
    try:
        fault = Path(context['fault'])
        temporary = fault.with_suffix('.tmp')
        temporary.write_text(json.dumps({'error': 'bootstrap_failed'}))
        temporary.replace(fault)
    finally:
        os._exit(70)
