#!/usr/bin/env python3
"""Fetch verified upstream inputs; inventory all tables and query statements."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
import urllib.request

LOCK = Path(__file__).with_name('inputs.lock.json')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(lock, destination):
    destination.mkdir(parents=True, exist_ok=False)
    for family, spec in [('generator', lock['generator']), ('spark', lock['queries'])]:
        archive = destination / (family + '.tar.gz')
        pin = spec['archive']
        with urllib.request.urlopen(pin['url'], timeout=120) as source, archive.open('xb') as output:
            remaining = pin['bytes']
            while block := source.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(block)
                if remaining < 0:
                    raise ValueError('archive exceeds pinned byte count')
                output.write(block)
        if remaining or sha(archive) != pin['sha256']:
            raise ValueError('archive differs from pin')
        selected = {k: v for k, v in lock['selected_inputs'].items() if k.startswith(family + '/')}
        with tarfile.open(archive) as tar:
            seen = set()
            for member in tar:
                relative = member.name.split('/', 1)[-1]
                if family == 'spark':
                    relative = relative.removeprefix(spec['path'] + '/')
                key = family + '/' + relative
                if key not in selected:
                    continue
                if not member.isfile() or key in seen or member.size > 4 * 1024**2:
                    raise ValueError('invalid selected archive member')
                path = destination / key
                if not path.resolve().is_relative_to(destination.resolve()):
                    raise ValueError('unsafe selected input path')
                data = tar.extractfile(member).read()
                if hashlib.sha256(data).hexdigest() != selected[key]:
                    raise ValueError('selected input differs from pin')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                seen.add(key)
        if seen != set(selected):
            raise ValueError('missing selected inputs')


def verify(lock, inputs):
    for relative, expected in lock['selected_inputs'].items():
        if sha(inputs / relative) != expected:
            raise ValueError('input differs from pin: ' + relative)


def schema(sql):
    """Parse only the pinned generator's bounded DDL dialect, failing closed."""
    sql = re.sub(r'--[^\n]*', '', sql)
    tables = []
    for statement in sql.split(';'):
        if not statement.strip():
            continue
        match = re.fullmatch(r'\s*create table (\w+)\s*\((.*)\)\s*', statement, re.I | re.S)
        if not match:
            raise ValueError('unrecognized DDL statement')
        name, body = match.groups()
        # Commas within decimal precision or composite keys do not delimit columns.
        fields = re.split(r',\s*(?![^()]*\))', body)
        columns, keys = [], []
        for field in fields:
            pk = re.fullmatch(r'\s*primary key\s*\(([^)]+)\)\s*', field, re.I)
            if pk:
                if keys:
                    raise ValueError('duplicate primary key')
                keys = [v.strip() for v in pk[1].split(',')]
                continue
            col = re.fullmatch(r'\s*(\w+)\s+(integer|date|time|char\(\d+\)|varchar\(\d+\)|decimal\(\d+,\d+\))\s*(not null)?\s*', field, re.I)
            if not col:
                raise ValueError('unrecognized column: ' + field)
            cname, kind, required = col.groups()
            columns.append(dict(name=cname, type=kind.lower(), nullable=not bool(required)))
        names = [c['name'] for c in columns]
        if len(names) != len(set(names)) or not set(keys).issubset(names):
            raise ValueError('invalid column/key inventory')
        tables.append(dict(name=name, columns=columns, primary_key=keys,
                           ddl=statement.strip() + ';'))
    if len({t['name'] for t in tables}) != len(tables):
        raise ValueError('duplicate table')
    return tables


def statements(text):
    """Split SQL outside literals/comments; preserve each complete executable query."""
    start = i = 0
    quote = None
    comment = 0
    line = False
    has_code = False
    output = []
    while i < len(text):
        c, pair = text[i], text[i:i+2]
        if line:
            if c == '\n': line = False
        elif comment:
            if pair == '/*': comment += 1; i += 1
            elif pair == '*/': comment -= 1; i += 1
        elif quote:
            if c == quote:
                if i + 1 < len(text) and text[i+1] == quote: i += 1
                else: quote = None
            elif c == '\\': i += 1
        elif pair == '--': line = True; i += 1
        elif pair == '/*': comment = 1; i += 1
        elif c in "'\"`": quote = c; has_code = True
        elif c == ';':
            if has_code: output.append(text[start:i].strip())
            start = i + 1; has_code = False
        elif not c.isspace(): has_code = True
        i += 1
    if quote or comment:
        raise ValueError('unterminated SQL literal/comment')
    if has_code: output.append(text[start:].strip())
    return output


def inventory(lock, inputs):
    verify(lock, inputs)
    all_tables = schema((inputs / 'generator/tools/tpcds.sql').read_text())
    metadata = [t for t in all_tables if t['name'] == lock['generator']['metadata_table']]
    tables = [t for t in all_tables if t not in metadata]
    if len(tables) != lock['generator']['expected_business_tables'] or len(metadata) != 1:
        raise ValueError('table inventory differs')
    queries = []
    for path in sorted((inputs / 'spark').glob('q*.sql')):
        match = re.fullmatch(r'q(\d+)([ab]?)\.sql', path.name)
        if not match:
            raise ValueError('unrecognized query name')
        parts = statements(path.read_text())
        if len(parts) != 1:
            raise ValueError('query file must contain exactly one statement')
        queries.append(dict(id=path.stem, template=int(match[1]), variant=match[2],
            source_sha256=sha(path), sql_sha256=hashlib.sha256(parts[0].encode()).hexdigest(),
            status='not_run', reason='EQ00 inventory; native-schema/product/reference gates pending',
            adaptation='Trim surrounding whitespace and remove terminal delimiter; retain SQL and literal substitutions'))
    if {q['template'] for q in queries} != set(range(1, 100)) or len(queries) != lock['queries']['expected_statements']:
        raise ValueError('full query denominator differs')
    return dict(scope='TPC-DS-derived engineering workload; no official score or execution claim',
        tables=tables, metadata_tables=metadata, queries=queries,
        template_count=99, statement_count=len(queries))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text())
    if args.fetch: fetch(lock, args.inputs)
    with args.report.open('x') as out: json.dump(inventory(lock, args.inputs), out, indent=2); out.write('\n')
