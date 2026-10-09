#!/usr/bin/env python3
"""One daemon-authorized read-only decode job, in a separate Python process."""
import os
from pathlib import Path
import sys
from capture.owner import private_json
from capture.spool import atomic
from incremental.lookahead import prepare

if __name__=='__main__':
    os.umask(0o077)
    config=private_json(Path(sys.argv[1]),4*1024*1024)
    try:prepare(config)
    except Exception:
        # No source values or exception text in process logs.
        atomic(Path(config['workspace'])/'result.json',dict(state='discarded',id=config['id']))
        sys.exit(1)
