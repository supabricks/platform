"""Read a local Delta snapshot through Arrow's native local filesystem."""

import json
from pathlib import Path
import sys

from deltalake import DeltaTable
import pyarrow.fs as fs


def local_rows(path):
    # Avoid the default Python-backed DeltaStorageHandler: short-lived processes
    # can abort during interpreter shutdown with its background Arrow I/O.
    # DeltaTable still chooses the active files/schema from the transaction log.
    path = str(Path(path).resolve())
    filesystem = fs.SubTreeFileSystem(path, fs.LocalFileSystem())
    return DeltaTable(path).to_pyarrow_table(filesystem=filesystem).to_pylist()


if __name__ == "__main__":
    print(json.dumps(sorted((r["id"], str(r["amount"])) for r in local_rows(sys.argv[1]))))
