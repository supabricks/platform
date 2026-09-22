"""Bounded pgoutput v1 decoder and reference model for SY00 only.

This is a qualification adapter, not a production capture/spool implementation.
Text values remain exact strings. Unknown protocol/schema events fail closed.
"""
from copy import deepcopy
from dataclasses import dataclass
import struct

MAX_MESSAGE = 1024 * 1024
MAX_BATCH = 16 * 1024 * 1024
MAX_TRANSACTION = 4 * 1024 * 1024
UNCHANGED = object()


class Unsupported(ValueError):
    pass


def lsn(value):
    high, low = value.split('/')
    return (int(high, 16) << 32) + int(low, 16)


def format_lsn(value):
    return f'{value >> 32:X}/{value & 0xffffffff:X}'


class Reader:
    def __init__(self, data):
        self.data = bytes(data)
        self.offset = 0
        if len(self.data) > MAX_MESSAGE:
            raise Unsupported('message budget exceeded')

    def take(self, size):
        if size < 0 or self.offset + size > len(self.data):
            raise Unsupported('truncated pgoutput message')
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def number(self, fmt):
        return struct.unpack('!' + fmt, self.take(struct.calcsize('!' + fmt)))[0]

    def string(self):
        end = self.data.find(b'\0', self.offset)
        if end < 0:
            raise Unsupported('unterminated pgoutput string')
        return self.take(end - self.offset + 1)[:-1].decode('utf-8', errors='strict')

    def tuple(self):
        count = self.number('H')
        if count > 128:
            raise Unsupported('column budget exceeded')
        result = []
        for _ in range(count):
            kind = self.take(1)
            if kind == b'n':
                result.append(None)
            elif kind == b'u':
                result.append(UNCHANGED)
            elif kind == b't':
                result.append(self.take(self.number('I')).decode('utf-8', errors='strict'))
            else:
                raise Unsupported('unsupported tuple encoding')
        return result

    def finish(self):
        if self.offset != len(self.data):
            raise Unsupported('trailing pgoutput bytes')


@dataclass(frozen=True)
class Transaction:
    xid: int
    commit_lsn: int
    end_lsn: int
    changes: tuple
    messages: tuple


class Decoder:
    def __init__(self, expected):
        # Qualified relation metadata: OID -> (namespace, name, identity, columns).
        self.expected = expected
        self.relations = {}

    def decode(self, payloads):
        transactions = []
        pending = None
        batch_bytes = 0
        last_end = 0
        for payload in payloads:
            batch_bytes += len(payload)
            if batch_bytes > MAX_BATCH:
                raise Unsupported('batch budget exceeded')
            r = Reader(payload)
            tag = r.take(1)
            if tag == b'B':
                if pending is not None:
                    raise Unsupported('nested transaction')
                final, timestamp, xid = r.number('Q'), r.number('q'), r.number('I')
                pending = dict(xid=xid, final=final, changes=[], messages=[], bytes=0)
            elif tag == b'R':
                oid, namespace, name = r.number('I'), r.string(), r.string()
                identity, count = r.take(1).decode(), r.number('H')
                if count > 128:
                    raise Unsupported('column budget exceeded')
                columns = tuple((r.number('B'), r.string(), r.number('I'), r.number('i')) for _ in range(count))
                relation = (namespace, name, identity, columns)
                if self.expected.get(oid) != relation:
                    raise Unsupported('schema or membership changed')
                self.relations[oid] = relation
            elif tag in (b'I', b'U', b'D'):
                if pending is None:
                    raise Unsupported('row outside transaction')
                oid = r.number('I')
                if oid not in self.relations:
                    raise Unsupported('unknown relation')
                marker = r.take(1)
                old = None
                if tag != b'I' and marker in (b'K', b'O'):
                    old = r.tuple()
                    marker = r.take(1) if tag == b'U' else None
                if tag == b'D':
                    if old is None:
                        raise Unsupported('delete lacks identity')
                    new = None
                else:
                    if marker != b'N':
                        raise Unsupported('row lacks new tuple')
                    new = r.tuple()
                width = len(self.relations[oid][3])
                if any(row is not None and len(row) != width for row in (old, new)):
                    raise Unsupported('tuple width changed')
                pending['changes'].append((tag.decode(), oid, old, new))
            elif tag == b'M':
                transactional, position = r.number('B'), r.number('Q')
                prefix, content = r.string(), r.take(r.number('I'))
                if transactional != 1 or pending is None:
                    raise Unsupported('unqualified nontransactional message')
                pending['messages'].append((prefix, content))
            elif tag == b'C':
                flags, commit, end, timestamp = r.number('B'), r.number('Q'), r.number('Q'), r.number('q')
                if pending is None or flags or commit != pending['final'] or end <= commit or end <= last_end:
                    raise Unsupported('invalid commit boundary')
                transactions.append(Transaction(pending['xid'], commit, end,
                                                tuple(pending['changes']), tuple(pending['messages'])))
                last_end, pending = end, None
            else:
                raise Unsupported('unsupported pgoutput message: ' + repr(tag))
            if pending is not None:
                pending['bytes'] += len(payload)
                if pending['bytes'] > MAX_TRANSACTION:
                    raise Unsupported('transaction budget exceeded')
            r.finish()
        if pending is not None:
            raise Unsupported('incomplete transaction batch')
        return transactions


class Model:
    """Atomic reference oracle; never a durable production checkpoint."""
    def __init__(self, identity, boundary, expected, rows):
        self.identity, self.boundary, self.expected = identity, boundary, expected
        self.rows = deepcopy(rows)
        self.applied = 0

    def apply(self, identity, transaction):
        if identity != self.identity:
            raise Unsupported('source lineage changed')
        if transaction.end_lsn <= self.boundary:
            return False
        if any(prefix != 'supabricks.sy00.barrier' for prefix, _ in transaction.messages):
            raise Unsupported('schema fence or unknown logical message')
        staged = deepcopy(self.rows)
        for action, oid, old, new in transaction.changes:
            columns = self.expected[oid][3]
            keys = [i for i, column in enumerate(columns) if column[0] & 1]
            if not keys:
                raise Unsupported('primary key required')
            values = old if old is not None else new
            key = tuple(values[i] for i in keys)
            if any(value is None or value is UNCHANGED for value in key):
                raise Unsupported('invalid key')
            table = staged[oid]
            if action in ('U', 'D') and key not in table:
                raise Unsupported('missing prior row')
            previous = table.get(key)
            if action == 'D':
                del table[key]
                continue
            row = []
            for i, value in enumerate(new):
                if value is UNCHANGED:
                    if previous is None:
                        raise Unsupported('unchanged column without prior row')
                    value = previous[i]
                row.append(value)
            new_key = tuple(row[i] for i in keys)
            if any(value is None for value in new_key):
                raise Unsupported('null primary key')
            if action == 'I' and new_key in table or action == 'U' and new_key != key and new_key in table:
                raise Unsupported('duplicate key')
            if action == 'U':
                del table[key]
            table[new_key] = row
        self.rows, self.boundary = staged, transaction.end_lsn
        self.applied += 1
        return True
