import tempfile
from pathlib import Path
import unittest
from diagnostics import summarize

class Diagnostics(unittest.TestCase):
    def test_failure_structure_survives_without_user_values(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.log'
            p.write_text('secret notebook SQL and credentials\n'+ 'x'*40000 +'\n  File "/secret/project/client.py", line 55, in execute\n    print("secret row")\nWebSocketTimeoutException: secret URL\n')
            result=summarize(p)
            self.assertEqual(result['frames'],[dict(file='client.py',line=55,function='execute')])
            self.assertEqual(result['failure_types'],['WebSocketTimeoutException'])
            self.assertTrue(result['truncated'])
            self.assertNotIn('secret',str(result))
            p.write_text('  File "/private/secret-project.py", line 99, in secret_function\nRuntimeError: password=secret\n')
            result=summarize(p)
            self.assertNotIn('secret',str(result))
            self.assertEqual(result['frames'],[dict(file='runtime',line=99,function='runtime')])
            p.unlink();p.symlink_to('/etc/passwd')
            self.assertFalse(summarize(p)['available'])

    def test_console_codes_survive_without_error_messages(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'private.log'
            p.write_text('RuntimeError: console workspace: HTTP 503: {"error":{"code":"io_error","retryable":false,"message":"Resource temporarily unavailable (os error 11); secret SQL"}}\n')
            result=summarize(p)
            self.assertEqual(result['console_errors'],[dict(status=503,code='io_error',retryable=False,io_kind='would_block')])
            self.assertNotIn('secret',str(result))
            p.write_text('RuntimeError: console workspace: HTTP 409: {"error":{"code":"secret","message":"secret"}}\n')
            result=summarize(p)
            self.assertEqual(result['console_errors'],[dict(status=409,code='unknown')])
            self.assertNotIn('secret',str(result))

if __name__=='__main__':unittest.main()
