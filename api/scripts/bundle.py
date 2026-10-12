#!/usr/bin/env python3
"""Print the specification as one JSON document.

With --protected, only operations whose contract may no longer change freely
(frozen, implemented or complete). CI compares that document from the base
branch with the full document from the change, so an incompatible change to a
protected operation fails.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'tests'))
from harness import spec  # noqa: E402

PROTECTED = ('frozen', 'implemented', 'complete')


def main():
    document = spec.bundle()
    if '--protected' in sys.argv:
        keep = {(o.path, o.method.lower()) for o in spec.operations() if o.stage in PROTECTED}
        document = dict(document, paths={
            path: {k: v for k, v in item.items() if k not in spec.METHODS or (path, k) in keep}
            for path, item in document['paths'].items() if any(p == path for p, _ in keep)})
    json.dump(document, sys.stdout, indent=1)


if __name__ == '__main__':
    main()
