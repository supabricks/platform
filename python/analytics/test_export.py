"""Small failure-boundary checks complement the real native PG suite."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from export import Rejected, arrow_type, boundary


class ExportBounds(unittest.TestCase):
    def test_decimal_contract_does_not_coerce_unsupported_precision_or_scale(self):
        self.assertEqual(str(arrow_type(1700, 4 + (38 << 16) + 8)), 'decimal128(38, 8)')
        for typmod in [-1, 4 + (39 << 16), 4 + (3 << 16) + 4, 4 + (3 << 16) + 2047]:
            with self.assertRaises(Rejected):
                arrow_type(1700, typmod)

    def test_space_and_deadline_are_checked_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=dict(deadline_ms=time.time()*1000+10000,limits=dict(max_bytes=1000))
            with patch('export.os.statvfs',return_value=SimpleNamespace(f_bavail=0,f_frsize=4096)):
                with self.assertRaisesRegex(Rejected,'free space'):
                    boundary(config,root,100)
            with self.assertRaisesRegex(Rejected,'output budget'):
                boundary(config,root,1001)
            config['deadline_ms']=0
            with self.assertRaisesRegex(Rejected,'deadline'):
                boundary(config,root)
            self.assertEqual(list(root.iterdir()),[])


if __name__=='__main__':unittest.main()
