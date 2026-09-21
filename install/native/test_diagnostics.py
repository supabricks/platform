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
            p.unlink();p.symlink_to('/etc/passwd')
            self.assertFalse(summarize(p)['available'])

if __name__=='__main__':unittest.main()
