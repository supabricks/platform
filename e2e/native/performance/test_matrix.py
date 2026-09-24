"""A runtime failure may continue only with matching reports and clean teardown."""
import unittest
from matrix import accepted


class Controller(unittest.TestCase):
    def entry(self,status='runtime_failed',exit_code=1,child_code=2):
        return dict(status=status,exit_code=exit_code,cleanup=dict(exit_code=child_code,
            remaining_descendants=0,leaked_descendants=0,timed_out=False))

    def test_gate_normalized_runtime_failure_can_continue(self):
        self.assertTrue(accepted(self.entry()))
        self.assertTrue(accepted(self.entry('measured',0,0)))

    def test_measurement_errors_and_inconsistent_reports_stop(self):
        for entry in (self.entry('error'),self.entry(child_code=1),
                      self.entry(exit_code=2),self.entry('measured',1,2)):
            self.assertFalse(accepted(entry))

    def test_cleanup_failure_always_stops(self):
        for key,value in (('timed_out',True),('leaked_descendants',1),('remaining_descendants',1)):
            entry=self.entry();entry['cleanup'][key]=value
            self.assertFalse(accepted(entry))
        self.assertFalse(accepted(dict(status='runtime_failed',exit_code=1)))


if __name__=='__main__':unittest.main()
