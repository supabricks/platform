#!/usr/bin/env python3
"""Compare the spec with the console prototype's feature list.

Every feature in the prototype's backing.ts should be served by at least one
operation (named in x-console-features). This prints, per section, how many
features the spec covers so far, and checks that x-backend and x-issues agree
with the prototype.

Usage: coverage.py [path/to/backing.ts]
"""
import pathlib, re, sys

api = pathlib.Path(__file__).resolve().parent.parent
default = api.parent.parent / 'console' / 'prototype' / 'src' / 'lib' / 'backing.ts'
backing = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else default
if not backing.exists():
    sys.exit(f'backing.ts not found at {backing}; pass its path')

features = {}
for m in re.finditer(r"\{ id: '([^']+)', section: '([^']+)', title: '((?:[^'\\]|\\.)*)', status: '(\w+)'(?:, issues: \[([\d, ]+)\])?", backing.read_text()):
    fid, section, title, status, issues = m.groups()
    features[fid] = dict(section=section, title=title, status=status, issues={int(i) for i in (issues or '').split(',') if i.strip()})

ops, served, spec_issues = 0, {}, {}
for f in sorted((api / 'domains').glob('*/paths.yaml')):
    for block in re.split(r'\n(?=  (?:get|post|put|patch|delete):)', f.read_text()):
        op = re.search(r'operationId: (\w+)', block)
        if not op: continue
        ops += 1
        ids = re.search(r'x-console-features: \[([^\]]*)\]', block)
        if not ids: print(f'  no x-console-features: {op.group(1)} ({f.parent.name})'); continue
        iss = re.search(r'x-issues: \[([^\]]*)\]', block)
        for fid in [i.strip() for i in ids.group(1).split(',') if i.strip()]:
            served.setdefault(fid, []).append(op.group(1))
            spec_issues.setdefault(fid, set()).update(int(i) for i in (iss.group(1).split(',') if iss else []) if i.strip())

unknown = sorted(set(served) - set(features))
sections = {}
for fid, f in features.items():
    s = sections.setdefault(f['section'], [0, 0, []])
    s[1] += 1
    if fid in served: s[0] += 1
    else: s[2].append(fid)

print(f'{ops} operations, {len(set(served) & set(features))} of {len(features)} prototype features covered\n')
for name, (done, total, missing) in sections.items():
    mark = 'done' if done == total else 'part' if done else '    '
    print(f'  {mark}  {name:<18} {done:>2}/{total:<2}  {" ".join(missing) if 0 < len(missing) <= 8 and done else ""}')
if unknown: print('\nFeature IDs in the spec that the prototype does not have:', ' '.join(unknown))
gaps = [fid for fid in served if fid in features and features[fid]['issues'] - spec_issues.get(fid, set())]
if gaps: print('\nPrototype issues not referenced by any operation serving the feature:', ' '.join(sorted(gaps)))
sys.exit(1 if unknown else 0)
