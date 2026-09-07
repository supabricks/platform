"""Ordinary PySpark shell attached to a Supabricks-owned epoch session."""
import argparse
import code
import json
import os
import time
from pathlib import Path
import socket


def main():
    os.environ['TZ'] = 'UTC'
    time.tzset()
    p = argparse.ArgumentParser()
    p.add_argument('--endpoint', required=True)
    p.add_argument('--root', required=True)
    p.add_argument('--binding', required=True)
    p.add_argument('--session', required=True)
    p.add_argument('--file', type=Path)
    args = p.parse_args()
    try:
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.remote(args.endpoint).getOrCreate()
        epoch = spark.sql('SELECT * FROM _supabricks.epoch').first().asDict()
        scope = {'spark': spark, 'epoch': epoch, '__name__': '__main__'}
        if args.file:
            scope['__file__'] = str(args.file)
            exec(compile(args.file.read_text(), str(args.file), 'exec'), scope)
        else:
            code.interact(banner=f"Supabricks epoch {epoch['epoch_id']}\nUse spark.table('public.orders'); metadata is available as epoch.", local=scope)
    finally:
        # Also runs when Ctrl-C terminates startup or a script. Runtime lifetime
        # remains the fallback for SIGKILL or a vanished daemon.
        request = {'version': 1, 'request': {'method': 'api', 'api_version': 1,
                   'binding': json.loads(args.binding),
                   'action': {'action': 'analytics_close', 'id': args.session}}}
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(5)
                sock.connect(str(Path(args.root) / 'control.sock'))
                sock.sendall(json.dumps(request).encode() + b'\n')
                sock.recv(65536)
        except OSError:
            pass


if __name__ == '__main__':
    main()
