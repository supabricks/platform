"""Adversarial managed-SQL admission and serialized output limits."""
import json
import unittest
import os
from unittest.mock import patch
from session import read_sql, query, validate_decimal_statistics, configure_catalog, frozen_aliases
from pathlib import Path
import tempfile


class SessionTests(unittest.TestCase):
    def test_frozen_catalog_rejects_mixed_sets_versions_and_collisions(self):
        tables=[dict(oid=1,schema='public',name='orders',version=0)]
        record=dict(schema='public',name='orders',uc_schema='analytics',uc_name='e_one',delta_version=0)
        catalog=dict(catalog='sb_test',tables=[record])
        self.assertEqual(len(frozen_aliases(catalog,tables)[1]),2)
        for bad in [[],[record,record],[dict(record,delta_version=1)],
                    [dict(record,name='foreign')],[dict(record,uc_schema='PUBLIC',uc_name='ORDERS')]]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                frozen_aliases(dict(catalog,tables=bad),tables)

    def test_provider_environment_is_explicit_and_credential_free(self):
        ambient={name:'poison' for name in ['SAIL_CATALOG__LIST','UC_TOKEN','UNITY_TOKEN',
                 'DATABRICKS_TOKEN','AWS_ACCESS_KEY_ID','AWS_PROFILE','AZURE_STORAGE_KEY',
                 'GOOGLE_APPLICATION_CREDENTIALS','GCS_TOKEN']}
        with patch.dict(os.environ,ambient,clear=True):
            configure_catalog(dict(catalog='sb_test'))
            self.assertEqual(set(os.environ),{'SAIL_CATALOG__LIST','SAIL_CATALOG__DEFAULT_CATALOG'})
            self.assertNotIn('poison',str(dict(os.environ)))
            self.assertIn('sb_test',os.environ['SAIL_CATALOG__LIST'])
            configure_catalog(None)
            self.assertNotIn('sb_test',os.environ['SAIL_CATALOG__LIST'])

    def test_multiple_binding_catalogs_are_explicit_and_collision_checked(self):
        with patch.dict(os.environ,{},clear=True):
            configure_catalog(None,[dict(catalog='dataset_sales'),dict(catalog='dataset_old')])
            self.assertIn('dataset_sales',os.environ['SAIL_CATALOG__LIST'])
            self.assertIn('dataset_old',os.environ['SAIL_CATALOG__LIST'])
            with self.assertRaises(ValueError):
                configure_catalog(None,[dict(catalog='dataset_sales'),dict(catalog='dataset_sales')])
            with self.assertRaises(ValueError):
                configure_catalog(None,[dict(catalog='spark_catalog')])

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
