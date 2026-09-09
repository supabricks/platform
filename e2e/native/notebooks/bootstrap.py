"""Initialize the ordinary ipykernel with a fully bound A03 Spark client."""
import json
import os
from pathlib import Path
from pyspark.sql import SparkSession
_context = json.loads(Path(os.environ['SB_N01_CONTEXT']).read_text())
spark = SparkSession.builder.remote(_context['endpoint']).getOrCreate()
epoch = spark.sql('SELECT * FROM _supabricks.epoch').first().asDict()
assert epoch['epoch_id'] == _context['epoch_id']
del _context
