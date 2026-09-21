"""A busy query plus a thread retaining an already-open admitted data descriptor."""
import threading
import time
from IPython import get_ipython
spark = get_ipython().user_ns["spark"]
from pathlib import Path
held_file = next(Path('/admitted/data100').glob('*.parquet')).open('rb')

def read_forever():
    counter = 0
    while True:
        held_file.seek(0)
        assert held_file.read(16)
        counter += 1
        Path('/scratch/open-file-heartbeat').write_text(str(counter))
        time.sleep(.05)

threading.Thread(target=read_forever,daemon=True).start()
Path('/scratch/query-started').write_text('running')
spark.sql('SELECT sum(sin(id)) FROM range(1000000000000)').collect()
raise RuntimeError('long-running query unexpectedly finished')
