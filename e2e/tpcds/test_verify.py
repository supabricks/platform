from datetime import date
from decimal import Decimal
import copy
import json
from pathlib import Path
import tempfile
import unittest

from compare import disposition
from verify import digest_rows, release_provenance
from inputs import sha


class ResultChecks(unittest.TestCase):
    def test_upgrade_requires_original_receipt_native_only_payload_and_completed_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);old=root/'old';new=root/'new';load=root/'load'
            old.mkdir();new.mkdir();(load/'state').mkdir(parents=True)
            manifest=dict(version='v1',files={'bin/supabricks':{'sha256':'old'},
                                             'worker.py':{'sha256':'worker'}},formats={'delta':2})
            def save(path,value):path.write_text(json.dumps(value))
            save(old/'release.json',manifest)
            receipt={'release_identity':sha(old/'release.json')}
            self.assertEqual(release_provenance(receipt,old,load),receipt)
            candidate=copy.deepcopy(manifest);candidate['version']='v2'
            candidate['files']['bin/supabricks']['sha256']='new'
            save(new/'release.json',candidate)
            upgrade={'from':{'identity':receipt['release_identity']},
                     'to':{'identity':sha(new/'release.json')},'backup_id':'backup-id','backup':'/backup'}
            save(load/'state/last-upgrade.json',upgrade)
            save(load/'state/runtime.json',{'installation_identity':sha(new/'release.json')})
            self.assertEqual(release_provenance(receipt,new,load,old)['load_release_identity'],receipt['release_identity'])
            with self.assertRaises(AssertionError):release_provenance(receipt,new,load)
            with self.assertRaises(AssertionError):release_provenance(receipt,new,load,new)
            (load/'state/upgrade.json').write_text('{}')
            with self.assertRaises(AssertionError):release_provenance(receipt,new,load,old)
            (load/'state/upgrade.json').unlink()
            upgrade['from']['identity']='unrelated';save(load/'state/last-upgrade.json',upgrade)
            with self.assertRaises(AssertionError):release_provenance(receipt,new,load,old)
            for field in ('worker','format'):
                changed=copy.deepcopy(candidate)
                if field=='worker':changed['files']['worker.py']['sha256']='different'
                else:changed['formats']['delta']=3
                save(new/'release.json',changed)
                with self.assertRaises(AssertionError):release_provenance(receipt,new,load,old)

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
