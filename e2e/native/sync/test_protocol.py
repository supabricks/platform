import struct
import unittest
from protocol import Decoder, Model, Transaction, Unsupported, UNCHANGED, MAX_MESSAGE

OID=42
REL=('public','orders','d',((1,'id',23,-1),(0,'note',25,-1)))
EXPECTED={OID:REL}
IDENTITY=('installation','branch-A','generation-1')


def begin(xid=4,commit=100):return b'B'+struct.pack('!QqI',commit,0,xid)
def end(commit=100,boundary=101):return b'C'+struct.pack('!BQQq',0,commit,boundary,0)
def relation():return b'R'+struct.pack('!I',OID)+b'public\0orders\0d'+struct.pack('!H',2)+b'\1id\0'+struct.pack('!Ii',23,-1)+b'\0note\0'+struct.pack('!Ii',25,-1)
def row(values):
    result=struct.pack('!H',len(values))
    for value in values:
        if value is None:result+=b'n'
        elif value is UNCHANGED:result+=b'u'
        else:
            data=value.encode();result+=b't'+struct.pack('!I',len(data))+data
    return result

def insert(values):return b'I'+struct.pack('!I',OID)+b'N'+row(values)


class ProtocolTests(unittest.TestCase):
    def model(self):return Model(IDENTITY,50,EXPECTED,{OID:{('1',):['1','original']}})

    def test_only_complete_commits_escape_decoder(self):
        decoder=Decoder(EXPECTED)
        payloads=[begin(),relation(),insert(['2','héllo']),end()]
        transactions=decoder.decode(payloads)
        self.assertEqual(transactions[0].end_lsn,101)
        model=self.model();model.apply(IDENTITY,transactions[0])
        self.assertEqual(model.rows[OID][('2',)],['2','héllo'])
        with self.assertRaises(Unsupported):decoder.decode(payloads[:-1])

    def test_every_truncated_message_fails(self):
        messages=[begin(),relation(),insert(['2','text']),end()]
        for number,message in enumerate(messages):
            for offset in range(len(message)):
                with self.subTest(number=number,offset=offset),self.assertRaises((Unsupported,UnicodeDecodeError)):
                    Decoder(EXPECTED).decode(messages[:number]+[message[:offset]])

    def test_unknown_schema_protocol_and_oversized_inputs(self):
        for payloads in ([b'T'+b'\0'*9],[b'S'],[begin(),relation()+b'x'],[begin(),b'I'+b'\0'*4+b'N'+row(['1','x'])],
                         [b'B'+b'0'*MAX_MESSAGE]):
            with self.assertRaises(Unsupported):Decoder(EXPECTED).decode(payloads)
        with self.assertRaises(Unsupported):Decoder({}).decode([begin(),relation(),end()])

    def test_commit_order_and_boundary_validation(self):
        for payloads in ([begin(),end(99,101)],[begin(),end(100,100)],
                         [begin(),end(),begin(5),end()]):
            with self.assertRaises(Unsupported):Decoder(EXPECTED).decode(payloads)
        # XID allocation order is unrelated to commit order.
        self.assertEqual([t.xid for t in Decoder(EXPECTED).decode([begin(5),end(),begin(4,200),end(200,201)])],[5,4])

    def test_replay_and_foreign_lineage(self):
        model=self.model();transaction=Transaction(4,100,101,(('I',OID,None,['2','new']),),())
        self.assertTrue(model.apply(IDENTITY,transaction))
        self.assertFalse(model.apply(IDENTITY,transaction))
        with self.assertRaises(Unsupported):model.apply(('other',),transaction)
        self.assertEqual(model.applied,1)

    def test_toast_key_change_null_and_delete(self):
        model=self.model()
        transaction=Transaction(4,100,101,(('U',OID,['1',None],['2',UNCHANGED]),),())
        model.apply(IDENTITY,transaction)
        self.assertEqual(model.rows[OID],{('2',):['2','original']})
        model.apply(IDENTITY,Transaction(5,200,201,(('U',OID,None,['2',None]),),()))
        self.assertIsNone(model.rows[OID][('2',)][1])
        model.apply(IDENTITY,Transaction(6,300,301,(('D',OID,['2',None],None),),()))
        self.assertEqual(model.rows[OID],{})

    def test_atomic_failure_preserves_rows_and_cursor(self):
        for changes in ((('I',OID,None,['2','valid']),('D',OID,['9',None],None)),
                        (('I',OID,None,['1','duplicate']),),
                        (('I',OID,None,[None,'null key']),),
                        (('I',OID,None,['2',UNCHANGED]),)):
            model=self.model()
            with self.assertRaises(Unsupported):model.apply(IDENTITY,Transaction(4,100,101,changes,()))
            self.assertEqual(model.boundary,50)
            self.assertEqual(model.rows[OID],{('1',):['1','original']})

    def test_schema_fence_without_rows_blocks_whole_group(self):
        model=self.model()
        with self.assertRaises(Unsupported):model.apply(IDENTITY,Transaction(4,100,101,(),(('supabricks.sy00.ddl',b'ALTER TABLE'),)))
        self.assertEqual(model.boundary,50)


if __name__=='__main__':unittest.main()
