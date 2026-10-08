from pathlib import Path
import hashlib
import json
import tempfile
import time
import unittest
from inputs import LOCK, schema, statements, verify
from generate import generation_profile, scan


class InputTests(unittest.TestCase):
    def test_native_composite_decimal_and_character_shape(self):
        table = schema('create table sales(a integer not null,b integer not null,v decimal(7,2),c char(4),d date,primary key(a,b));')[0]
        self.assertEqual(table['primary_key'], ['a', 'b'])
        self.assertEqual([c['type'] for c in table['columns']], ['integer','integer','decimal(7,2)','char(4)','date'])
        self.assertFalse(table['columns'][0]['nullable'])
        self.assertTrue(table['columns'][2]['nullable'])

    def test_unknown_ddl_and_missing_key_columns_are_not_silently_dropped(self):
        for sql in ['drop table sales;', 'create table t(a money);',
                    'create table t(a integer,primary key(b));',
                    'create table t(a integer,a integer);']:
            with self.subTest(sql=sql), self.assertRaises(ValueError): schema(sql)

    def test_statement_delimiters_do_not_split_literals_or_comments(self):
        self.assertEqual(len(statements("-- ;\nselect ';', 'it''s;'; /* ; /* nested */ */ select 2;")), 2)
        self.assertEqual(statements('select 1; -- end\n'), ['select 1'])
        self.assertEqual(statements('select 1; /* /* nested */ tail */'), ['select 1'])
        for sql in ["select 'bad", 'select 1 /* bad']:
            with self.assertRaises(ValueError): statements(sql)

    def test_modified_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp);(path/'x').write_text('changed')
            with self.assertRaises(ValueError): verify({'selected_inputs': {'x':'0'*64}},path)

    def test_generated_rows_reject_truncation_and_schema_drift(self):
        columns = [dict(nullable=False), dict(nullable=True)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'table.dat'
            path.write_bytes(b'1|value|\n2||\n')
            result = scan(path, columns)
            self.assertEqual(result['rows'], 2)
            self.assertEqual(result['empty_fields_by_column'], [0, 1])
            self.assertEqual(result['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            with self.assertRaises(TimeoutError):
                scan(path, columns, time.monotonic() - 1)
            for invalid in [b'', b'1|value|', b'1|value|extra|\n', b'|value|\n']:
                path.write_bytes(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(ValueError): scan(path, columns)

    def test_large_generation_is_explicit_and_preserves_original_pin(self):
        lock = json.loads(LOCK.read_text())
        scale, bounds, profile = generation_profile('sf1', lock)
        self.assertEqual(scale, 1)
        self.assertEqual(bounds, lock['pilot'])
        self.assertIsNone(profile)
        scale, bounds, profile = generation_profile('sf100', lock)
        self.assertEqual(scale, 100)
        self.assertGreaterEqual(bounds['minimum_free_gib'], bounds['maximum_generated_gib'] + bounds['minimum_remaining_free_gib'])
        self.assertEqual(len(profile['sha256']), 64)
        self.assertEqual(lock['generator']['scale'], 1)
        with self.assertRaises(ValueError): generation_profile('unbounded', lock)


if __name__ == '__main__': unittest.main()
