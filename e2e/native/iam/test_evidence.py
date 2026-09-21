"""Regression gates for incomplete or misleading capability reports."""
import copy
import json
from pathlib import Path
import unittest
from qualify import validate

EVIDENCE=Path(__file__).resolve().parents[3]/'docs/architecture/iam00-evidence/linux-x86_64.json'


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report=json.loads(EVIDENCE.read_text())

    def test_recorded_capabilities(self):
        validate(self.report)

    def test_missing_security_check_rejected(self):
        report=copy.deepcopy(self.report)
        report['evidence']['checks']=[c for c in report['evidence']['checks'] if c['name']!='pg_denies_superuser']
        with self.assertRaises(AssertionError): validate(report,False)

    def test_slow_revocation_rejected(self):
        report=copy.deepcopy(self.report)
        report['evidence']['metrics']['bob']['revocation']['seconds']=61
        with self.assertRaises(AssertionError): validate(report,False)

    def test_product_governance_claim_rejected(self):
        report=copy.deepcopy(self.report); report['governed_product_enabled']=True
        with self.assertRaises(AssertionError): validate(report,False)

    def test_component_drift_rejected(self):
        report=copy.deepcopy(self.report); report['pins']['gvisor']['version']='latest'
        with self.assertRaises(AssertionError): validate(report,False)

    def test_failed_cleanup_rejected(self):
        report=copy.deepcopy(self.report); report['cleanup'][0]['status']='FAIL'
        with self.assertRaises(AssertionError): validate(report,False)

    def test_missing_external_denials_rejected(self):
        report=copy.deepcopy(self.report)
        report['evidence']['checks']=[c for c in report['evidence']['checks'] if not c['name'].startswith('bob:tcp_denied:127.0.0.1:')]
        with self.assertRaises(AssertionError): validate(report,False)

    def test_changed_source_rejected(self):
        report=copy.deepcopy(self.report); report['source_sha256']['e2e/native/iam/controller.py']='0'*64
        with self.assertRaises(AssertionError): validate(report)


if __name__=='__main__': unittest.main()
