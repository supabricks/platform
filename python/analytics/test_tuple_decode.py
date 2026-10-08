"""Strict tuple framing and scalar boundaries for the allocation-reduced decoder."""
from datetime import date
from decimal import Decimal
import struct
import unittest
from capture.protocol import Reader
from capture.spool import CaptureError
from incremental.rows import tuple_values,value,UNCHANGED


class TupleDecode(unittest.TestCase):
    def tuple(self,fields):
        return struct.pack('!H',len(fields))+b''.join(
            b'u' if field is UNCHANGED else b'n' if field is None else
            b't'+struct.pack('!I',len(field))+field for field in fields)

    def test_mixed_lossless_values_and_exact_reader_boundary(self):
        columns=[[1,'id',20,-1],[0,'n',1700,((38<<16)|8)+4],
                 [0,'label',1042,9],[0,'day',1082,-1],[0,'null',25,-1],[0,'toast',25,-1]]
        data=self.tuple([b'-9223372036854775808',b'12345678901234567890.12345678',
                         'café '.encode(),b'2026-10-08',None,UNCHANGED])
        reader=Reader(data+b'!')
        self.assertEqual(tuple_values(reader,columns),[-9223372036854775808,
                         Decimal('12345678901234567890.12345678'),'café ',date(2026,10,8),None,UNCHANGED])
        self.assertEqual(reader.offset,len(data))
        with self.assertRaises(CaptureError):reader.finish()
        self.assertEqual(reader.take(1),b'!');reader.finish()
        for length in range(len(data)):
            with self.subTest(length=length),self.assertRaises(CaptureError):
                tuple_values(Reader(data[:length]),columns)

    def test_invalid_lengths_kinds_utf8_schema_and_row_budget_fail_closed(self):
        column=[[0,'v',25,-1]]
        cases=[b'\x00\x01t\xff\xff\xff\xff',b'\x00\x01b',
               self.tuple([b'\xff']),self.tuple([b'a',b'b']),self.tuple([b'x'*(256*1024+1)])]
        for data in cases:
            with self.assertRaises(CaptureError):tuple_values(Reader(data),column)

    def test_integer_bounds_and_decimal_exactness_are_unchanged(self):
        for typ,bits in [(21,16),(23,32),(20,64)]:
            column=[1,'id',typ,-1]
            for n in [-(1<<(bits-1)),(1<<(bits-1))-1]:self.assertEqual(value(str(n),column),n)
            for n in [-(1<<(bits-1))-1,1<<(bits-1)]:
                with self.assertRaises(CaptureError):value(str(n),column)
        numeric=[0,'n',1700,((5<<16)|2)+4]
        self.assertEqual(value('999.99',numeric),Decimal('999.99'))
        for raw in ['1000.00','1.001','NaN','Infinity','-Infinity']:
            with self.assertRaises(CaptureError):value(raw,numeric)

    def test_fixed_width_reader_handles_boundaries_and_signed_values(self):
        for fmt,number in [('B',255),('H',65535),('I',2**32-1),('Q',2**64-1),('q',-2**63)]:
            encoded=struct.pack('!'+fmt,number)
            reader=Reader(encoded);self.assertEqual(reader.number(fmt),number);reader.finish()
            for length in range(len(encoded)):
                with self.assertRaises(CaptureError):Reader(encoded[:length]).number(fmt)


if __name__=='__main__':unittest.main()
