import copy
import json
from pathlib import Path
import unittest

from inputs import LOCK, sha
from workload import workload, validate_generation, require_storage


class WorkloadAdmission(unittest.TestCase):
    def setUp(self):
        self.profile = workload('sf100')
        self.receipt = Path(__file__).resolve().parents[2] / 'docs/architecture/tpcds-evidence/2026-10-08-sf100-generation/generation.json'
        self.generation = json.loads(self.receipt.read_text())
        self.tables = [dict(name=t['file'].removesuffix('.dat')) for t in self.generation['tables']]

    def validate(self, generation=None, profile=None, receipt_hash=None):
        return validate_generation(profile or self.profile, generation or self.generation,
                                   receipt_hash or sha(self.receipt), sha(LOCK), self.tables)

    def test_sf100_binds_actual_validated_inventory_and_receipt(self):
        self.assertEqual(len(self.validate()), 24)
        with self.assertRaises(ValueError): self.validate(receipt_hash='0'*64)
        for key, value in [('scale', 1), ('business_rows', 19557335), ('status', 'FAIL'),
                           ('input_lock_sha256', 'wrong'), ('generation_profile', {})]:
            with self.subTest(key=key):
                changed = copy.deepcopy(self.generation); changed[key] = value
                with self.assertRaises(ValueError): self.validate(changed)

    def test_inventory_duplicates_missing_rows_and_sf1_reject_sf100(self):
        with self.assertRaises(ValueError): self.validate(profile=workload('sf1'))
        for mutation in ('duplicate', 'missing', 'rows'):
            changed = copy.deepcopy(self.generation)
            if mutation == 'duplicate': changed['tables'].append(changed['tables'][0])
            elif mutation == 'missing': changed['tables'].pop()
            else: changed['tables'][0]['rows'] += 1
            with self.assertRaises(ValueError): self.validate(changed)

    def test_storage_admission_and_sampled_stop_boundaries(self):
        gib = 1024**3
        require_storage(self.profile, 656*gib, admission=True)
        require_storage(self.profile, 64*gib, 512*gib)
        for free, size, admission in [(656*gib-1, 0, True), (64*gib-1, 0, False),
                                       (100*gib, 512*gib+1, False)]:
            with self.assertRaises(RuntimeError):
                require_storage(self.profile, free, size, admission=admission)

    def test_legacy_sf1_receipt_without_scale_stays_supported(self):
        profile = workload('sf1')
        receipt = dict(status='PASS', input_lock_sha256=sha(LOCK), business_rows=19557335,
                       tables=[dict(file='t.dat', rows=19557335)])
        validate_generation(profile, receipt, 'legacy', sha(LOCK), [dict(name='t')])
        require_storage(profile, 80*1024**3, admission=True)
        require_storage(profile, 16*1024**3, 64*1024**3)
        self.assertEqual(profile['load_bounds']['timeout_seconds'], 7200)
        self.assertEqual(profile['load_bounds']['default_max_unpublished_rows'], 0)


if __name__ == '__main__': unittest.main()
