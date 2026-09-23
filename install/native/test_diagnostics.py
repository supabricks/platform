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
            p.write_text('NOTEBOOK_READINESS_TRANSIENT_503\n')
            self.assertEqual(summarize(p)['readiness_poll_503_observed'],1)
            p.write_text('RuntimeError: console workspace: HTTP 409: {"error":{"code":"secret","message":"secret"}}\n')
            result=summarize(p)
            self.assertEqual(result['console_errors'],[dict(status=409,code='unknown')])
            self.assertNotIn('secret',str(result))

    def test_native_codes_and_postgres_states_exclude_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.log'
            path.write_text("RuntimeError: {'code': 'conflict', 'retryable': False, 'message': 'governed PostgreSQL rejected request (55P03)', 'private': 'secret'}\n")
            result=summarize(path)
            self.assertEqual(result['native_errors'],[dict(code='conflict',retryable=False,postgres_state='55P03')])
            self.assertNotIn('secret',str(result))
            path.write_text("RuntimeError: {'code': 'conflict', 'detail': {'hint': 'secret', 'message': 'governed PostgreSQL rejected request (57P01)', 'retryable': False}}\n")
            result=summarize(path)
            self.assertEqual(result['native_errors'],[dict(code='conflict',retryable=False,postgres_state='57P01')])
            self.assertNotIn('secret',str(result))
            path.write_text("RuntimeError: {'code': 'conflict', 'detail': 'secret'}\n")
            self.assertEqual(summarize(path)['native_errors'],[dict(code='conflict')])
            path.write_text("RuntimeError: {'code': 'secret', 'message': 'secret SQL and credentials'}\n")
            self.assertEqual(summarize(path)['native_errors'],[dict(code='unknown')])
            path.write_text("RuntimeError: {'code': 'conflict', 'message': 'governed PostgreSQL rejected request (HELLO)'}\n")
            self.assertEqual(summarize(path)['native_errors'],[dict(code='conflict',postgres_state='other')])

if __name__=='__main__':unittest.main()
