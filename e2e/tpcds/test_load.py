import tempfile
from pathlib import Path
import unittest

from load import batches, country_control


class CopyBatches(unittest.TestCase):
    def test_country_control_uses_this_scale_customer_key_and_latin1(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'customer.dat'
            path.write_bytes(b'4|CANADA|\n900|R\xc9UNION|\n')
            columns = [dict(name='c_customer_sk'), dict(name='c_birth_country')]
            self.assertEqual(country_control(path, columns), (900, 'RÉUNION'))
            path.write_bytes(b'4|CANADA|\n')
            with self.assertRaises(ValueError): country_control(path, columns)

    def converted(self, data, **limits):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'table.dat'; path.write_bytes(data)
            return list(batches(path, [dict(nullable=False), dict(nullable=True)], **limits))

    def test_lossless_copy_text_and_null_encoding(self):
        data = b'1|\\N\t\r|\n2||\n3|caf\xc3\xa9|\n'
        result = self.converted(data)
        self.assertEqual(result, [(0, len(data), 3, b'1\t\\\\N\\t\\r\n2\t\\N\n3\tcaf\xc3\xa9\n')])

    def test_byte_limit_applies_after_escaping_and_offsets_are_original(self):
        data = b'1|\\\\|\n2|x|\n3|y|\n'
        result = self.converted(data, max_bytes=8, max_rows=2)
        self.assertEqual([r[2] for r in result], [1, 2])
        self.assertEqual([r[:2] for r in result], [(0, 6), (6, len(data))])
        self.assertTrue(all(len(r[3]) <= 8 for r in result))

    def test_row_ceiling_and_incomplete_or_required_values_fail(self):
        self.assertEqual([r[2] for r in self.converted(b'1|x|\n2|y|\n', max_rows=1)], [1, 1])
        for data in (b'1|x', b'1|x|extra|\n', b'|x|\n'):
            with self.assertRaises(ValueError): self.converted(data)
        with self.assertRaises(ValueError): self.converted(b'1|\\\\|\n', max_bytes=3)


if __name__ == '__main__': unittest.main()
