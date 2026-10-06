import unittest
from unittest.mock import Mock
from qualify import wait_for_expired_source


class ExpiryQualification(unittest.TestCase):
    def bounded(self, probe, seconds):
        self.assertEqual(seconds,20)
        for _ in range(3):
            if probe():return
        raise AssertionError('condition timed out')

    def test_read_timeout_requires_subsequent_expiry_rejection(self):
        for errno in (11,35):
            with self.subTest(errno=errno):
                browser=Mock(side_effect=[(503,{'error':{'code':'io_error','message':f'Resource temporarily unavailable (os error {errno})'}}),
                                          (409,{'error':{'message':'source expired'}})])
                self.assertEqual(wait_for_expired_source(browser,'fixture-source',self.bounded),1)
                self.assertEqual(browser.call_count,2)
                for call in browser.call_args_list:
                    self.assertEqual(call.args,({'action':'source','source':'fixture-source'},))

    def test_wrong_rejection_or_success_is_never_accepted(self):
        for response in ((200,{}),(409,{'error':{'message':'another conflict'}}),
                         (503,{'error':{'code':'io_error','message':'Permission denied'}})):
            with self.subTest(response=response):
                browser=Mock(return_value=response)
                with self.assertRaises(AssertionError):wait_for_expired_source(browser,'source',self.bounded)
                browser.assert_called_once()

    def test_persistent_timeout_still_fails(self):
        browser=Mock(return_value=(503,{'error':{'code':'io_error','message':'Resource temporarily unavailable (os error 35)'}}))
        with self.assertRaisesRegex(AssertionError,'condition timed out'):
            wait_for_expired_source(browser,'source',self.bounded)


if __name__=='__main__':unittest.main()
