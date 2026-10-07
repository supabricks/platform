from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from compare import disposition
from verify import digest_rows


class ResultChecks(unittest.TestCase):
    def test_partition_digests_preserve_nulls_padding_and_duplicate_multiplicity(self):
        rows=[(1,Decimal('1.25'),date(2000,2,29),'x   ',None),
              (2,Decimal('0.00'),None,'CÔTE D\'IVOIRE','')]
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            a=digest_rows(rows,root/'a');b=digest_rows(reversed(rows),root/'b')
            self.assertEqual(a,b)
            self.assertNotEqual(a,digest_rows(rows+[rows[0]],root/'duplicate'))
            changed=[(1,Decimal('1.25'),date(2000,2,29),'x',None),rows[1]]
            self.assertNotEqual(a,digest_rows(changed,root/'trimmed'))
            changed=[rows[0],(2,Decimal('0.00'),None,'CÔTE D\'IVOIRE',None)]
            self.assertNotEqual(a,digest_rows(changed,root/'null'))

    def test_query_comparison_never_hides_order_type_or_value_differences(self):
        def result(rows,kind='string'):
            return dict(schema=dict(fields=[dict(type=kind)]),values=rows)
        a=result([['1.0000000000000001'],[None]])
        self.assertEqual(disposition(a,a),'correct')
        self.assertEqual(disposition(a,result(list(reversed(a['values'])))),'order_review_required')
        self.assertEqual(disposition(a,result([['1.0'],[None]])),'result_mismatch_review_required')
        self.assertEqual(disposition(a,result(a['values'],'double')),'type_mismatch')
        self.assertEqual(disposition(result([['x'],['x']]),result([['x']])),
                         'result_mismatch_review_required')


if __name__=='__main__':unittest.main()
