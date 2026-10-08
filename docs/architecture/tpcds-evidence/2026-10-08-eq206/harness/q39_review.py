#!/usr/bin/env python3
"""Explicit numerical review of the retained SF1 q39 results (#206).

The general strict comparator is unchanged. This review accepts only the pinned
fixture/SQL, exact types, ordered row membership and means, with two ULPs from an
independent 80-digit oracle for CV columns 4 and 9. 160 digits must round identically.
The bound is a finite-fixture engineering contract, not a general variance guarantee.
"""
import argparse
from collections import Counter
from decimal import Decimal, localcontext
from fractions import Fraction
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct

from compare import run as compare

BOUND_ULPS = 2
CONTRACT = Path(__file__).with_name('q39-review.lock.json')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    return gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_bytes()


def moments(groups):
    stats = {}
    for group in groups:
        key = tuple(group['key'])
        require(len(key) == 3 and all(type(k) is int for k in key), 'invalid group key')
        require(key not in stats, 'duplicate group')
        values = [value for _, value in group['rows'] if value is not None]
        require(all(type(v) is int and v >= 0 for v in values), 'unsupported quantity')
        n, total = len(values), sum(values)
        stats[key] = None
        if n < 2 or total == 0:
            continue
        ss = sum(v*v for v in values)
        numerator = n * (n*ss - total*total)
        denominator = (n-1)*total*total
        stats[key] = dict(n=n, total=total, sum_squares=ss,
                          numerator=numerator, denominator=denominator)
    return stats


def selected(stats, query):
    require(query in ('q39a', 'q39b'), 'review only applies to q39a/q39b')
    result = []
    for (warehouse, item, month), first in stats.items():
        second = stats.get((warehouse, item, 2))
        if month != 1 or first is None or second is None:
            continue
        if any(s['numerator'] <= s['denominator'] for s in (first, second)):
            continue
        if query == 'q39b' and 4*first['numerator'] <= 9*first['denominator']:
            continue
        result.append((warehouse, item))
    # Both ORDER BY lists start with warehouse/item, a unique pair here.
    return sorted(result)


def oracle(stat):
    with localcontext() as context:
        context.prec = 80
        value = (Decimal(stat['numerator']) / stat['denominator']).sqrt()
    with localcontext() as context:
        context.prec = 160
        check = (Decimal(stat['numerator']) / stat['denominator']).sqrt()
    require(float(value) == float(check), 'oracle precision is insufficient')
    nearest = float(value)
    require(math.isfinite(nearest) and nearest > 0, 'unsupported oracle value')
    center = Fraction.from_float(nearest)
    lower = (Fraction.from_float(math.nextafter(nearest, -math.inf)) + center)/2
    upper = (Fraction.from_float(math.nextafter(nearest, math.inf)) + center)/2
    exact_squared = Fraction(stat['numerator'], stat['denominator'])
    # Certify the rounding interval with exact rationals, independently of Decimal.
    # An exact midpoint is deliberately review-required rather than guessing a tie.
    require(lower*lower < exact_squared < upper*upper, 'nearest-double interval not certified')
    return str(value), nearest


def ulps(actual, expected):
    require(math.isfinite(actual) and actual > 0, 'CV must be finite and positive')
    bits = lambda value: struct.unpack('>Q', struct.pack('>d', value))[0]
    return abs(bits(actual) - bits(expected))


def review_rows(rows, stats, query):
    keys = selected(stats, query)
    require(len(rows) == len(keys), 'row membership/count differs from integer oracle')
    cells = []
    for index, (row, pair) in enumerate(zip(rows, keys)):
        require(len(row) == 10, 'q39 requires ten columns')
        for offset, month in ((0, 1), (5, 2)):
            # The evidence JSON encodes SQL integer/double values as strings.
            key = (*pair, month)
            require(row[offset:offset+3] == list(map(str, key)), 'keys/order differ from oracle')
            stat = stats[key]
            require(row[offset+3] == str(stat['total']/stat['n']), 'mean differs from exact oracle')
            decimal80, nearest = oracle(stat)
            distance = ulps(float(row[offset+4]), nearest)
            require(distance <= BOUND_ULPS, f'{query} row {index} column {offset+4} exceeds {BOUND_ULPS} ULP')
            cells.append(dict(row=index, column=offset+4, key=key, decimal80=decimal80,
                              nearest_double=nearest, actual=row[offset+4], oracle_ulps=distance))
    return dict(rows=len(rows), cells=cells, oracle_ulps=dict(Counter(c['oracle_ulps'] for c in cells)))


def validate_query(query, sql_hash, engine):
    require(query['sql_sha256'] == sql_hash, 'SQL changed')
    require([f['type'] for f in query['schema']['fields']] ==
            ['integer']*3+['double']*2+['integer']*3+['double']*2, 'types changed')
    status = 'complete' if engine == 'spark' else 'complete_requires_reference_comparison'
    require(query['status'] == status, 'query incomplete')


def run(product, reference, groups, strict, output):
    require(not output.exists(), 'fresh output directory required')
    contract = json.loads(CONTRACT.read_text())
    raw = read_bytes(groups)
    require(digest(raw) == contract['groups_sha256'], 'group fixture hash mismatch')
    fixture = json.loads(raw)
    for key in ('load_receipt_sha256', 'epoch_id', 'dimension_key_counts'):
        require(fixture[key] == contract[key], f'fixture {key} mismatch')
    require(len(fixture['groups']) == 90000, 'incomplete SF1 group population')
    require(sum(len(g['rows']) for g in fixture['groups']) == 360000, 'incomplete SF1 inventory population')
    stats = moments(fixture['groups'])
    actual = json.loads((product/'result.json').read_text())
    expected = json.loads((reference/'result.json').read_text())
    require(digest((reference/'result.json').read_bytes()) == contract['reference_sha256'], 'reference changed')
    for key in ('load_receipt_sha256', 'epoch_id'):
        require(actual[key] == contract[key], f'product {key} mismatch')
    require(actual['stopped'], 'product session was not stopped')
    output.mkdir(parents=True)
    # Recompute instead of trusting a supplied strict ledger or its declared counts.
    compare(product, reference, output/'strict-recomputed.json')
    ledger = json.loads(strict.read_text())
    require(ledger == json.loads((output/'strict-recomputed.json').read_text()), 'strict ledger changed')
    queries = []
    for query in ('q39a', 'q39b'):
        result = dict(id=query, status='NUMERICALLY_ACCEPTED', bound_ulps=BOUND_ULPS)
        for label, root, receipt in (('product', product, actual), ('spark', reference, expected)):
            q = next(q for q in receipt['queries'] if q['id'] == query)
            validate_query(q, contract['sql_sha256'][query], label)
            rows = [json.loads(line) for line in (root/'queries'/(query+'.rows.jsonl')).read_text().splitlines()]
            result[label] = review_rows(rows, stats, query)
        queries.append(result)
    remaining = [q for q in ledger['queries'] if q['id'] not in ('q39a', 'q39b') and q['status'] != 'correct']
    report = dict(status='Q39_NUMERICALLY_ACCEPTED',
                  scope='Retained SF1 only; raw strict ledger unchanged; not an official TPC score',
                  contract_sha256=digest(CONTRACT.read_bytes()), fixture_sha256=digest(Path(__file__).read_bytes()),
                  groups_sha256=digest(raw), strict_sha256=digest(strict.read_bytes()),
                  strict_counts=ledger['counts'], numerically_accepted_queries=2,
                  other_strict_review_required=remaining, queries=queries)
    (output/'review.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('product', 'reference', 'groups', 'strict', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    print(run(**vars(args))['status'])
