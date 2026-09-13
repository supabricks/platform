"""Adversarial managed-SQL admission and serialized output limits."""
import json
import unittest
from session import read_sql, query, validate_decimal_statistics
from pathlib import Path
import tempfile


class SessionTests(unittest.TestCase):
    def test_read_gate_handles_quotes_comments_and_mutating_ctes(self):
        for sql in ["SELECT 'delete; drop'", 'SELECT `delete` FROM `public`.`orders`',
                    '/* nested /* drop */ comment */ WITH t AS (SELECT 1) SELECT * FROM t',
                    'EXPLAIN SELECT 1', 'SELECT 1 -- ; DROP TABLE orders']:
            self.assertEqual(read_sql(sql),sql)
        for sql in ['DELETE FROM orders', 'SELECT 1; SELECT 2',
                    'WITH t AS (SELECT 1) INSERT INTO orders SELECT * FROM t',
                    "SELECT 'x'; /* safe */ DROP VIEW orders", 'EXPLAIN DELETE FROM orders',
                    "SELECT TRANSFORM(id) USING 'sh' FROM orders", 'SELECT 1 /*',
                    'SELECT `unterminated', "SELECT 'unterminated"]:
            with self.subTest(sql=sql),self.assertRaises(ValueError): read_sql(sql)

    def test_old_decimal_statistics_fail_before_catalog_creation(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);log=root/'101/_delta_log';log.mkdir(parents=True)
            table=dict(path='101',version=0,columns=[dict(name='amount',type_oid=1700)])
            file=log/'00000000000000000000.json'
            file.write_text(json.dumps({'add':{'stats':json.dumps({'minValues':{'amount':1234567890123456.0}})}})+'\n')
            with self.assertRaisesRegex(ValueError,'analytics refresh'):validate_decimal_statistics(root,[table])
            file.write_text(json.dumps({'add':{'stats':json.dumps({'numRecords':1,'minValues':{},'maxValues':{}})}})+'\n')
            validate_decimal_statistics(root,[table])

    def test_wire_budget_includes_escaped_values_and_metadata(self):
        class Type:
            def simpleString(self):return 'string'
        class Field:
            name='text';dataType=Type()
        class Schema:fields=[Field()]
        class Frame:
            schema=Schema()
            def sql(self,sql):return self
            def limit(self,n):self.n=n;return self
            def toLocalIterator(self):return iter([['\\"\n'*100] for _ in range(self.n)])
        for budget in (1024,2048,4096):
            result=query(Frame(),dict(id='query',sql='SELECT x',max_rows=10,max_bytes=budget),dict(session_id='session',epoch_id='epoch'))
            self.assertLessEqual(len(json.dumps(result).encode()),budget)
            self.assertTrue(result['truncated'])
            self.assertEqual(result['sql'], 'SELECT x')
        with self.assertRaisesRegex(ValueError, 'byte budget'):
            query(Frame(),dict(id='query',sql='SELECT x /*'+'x'*2048+'*/',max_rows=10,max_bytes=1024),dict(session_id='session',epoch_id='epoch'))


if __name__=='__main__':unittest.main()
