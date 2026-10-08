from pathlib import Path
import tempfile
import unittest
from inputs import schema, statements, verify
from generate import scan


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
            for invalid in [b'', b'1|value|', b'1|value|extra|\n', b'|value|\n']:
                path.write_bytes(invalid)
                with self.subTest(invalid=invalid), self.assertRaises(ValueError): scan(path, columns)


if __name__ == '__main__': unittest.main()
