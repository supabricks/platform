"""SY02 durable-write/ack boundary, corruption and decoder qualification."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from capture.spool import Spool, CaptureError, MAX_MESSAGE, MAX_TRANSACTION, frames
from capture.protocol import Decoder, Wire

IDENTITY = dict(generation='test', timeline_id='a', decoder_version=1)
SCHEMA = {'42': ['public','orders','d',[[1,'id',23,-1],[0,'value',25,-1]]]}

def begin(commit=200):return b'B'+struct.pack('!QqI',commit,0,123)
def commit(at=200,end=210):return b'C'+struct.pack('!BQQq',0,at,end,0)
def relation():return b'R'+struct.pack('!I',42)+b'public\0orders\0d'+struct.pack('!H',2)+b'\1id\0'+struct.pack('!Ii',23,-1)+b'\0value\0'+struct.pack('!Ii',25,-1)
def insert(value=b'value'):return b'I'+struct.pack('!I',42)+b'N'+struct.pack('!H',2)+b't'+struct.pack('!I',1)+b'1t'+struct.pack('!I',len(value))+value

class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'spool'
    def tearDown(self):self.temp.cleanup()
    def spool(self):return Spool(self.root,IDENTITY,16*1024*1024)
    def test_complete_commit_only_and_reconnect_stable_checksum(self):
        d=Decoder(SCHEMA,'fence')
        for p in (begin(),relation(),insert()):self.assertIsNone(d.feed(p))
        tx=d.feed(commit());self.assertEqual([p[:1] for p in frames(tx[2])],[b'B',b'I',b'C'])
        d.feed(begin(300));d.feed(insert());first=d.feed(commit(300,310))
        reconnect=Decoder(SCHEMA,'fence')
        for p in (begin(300),relation(),insert()):reconnect.feed(p)
        self.assertEqual(first,reconnect.feed(commit(300,310)))
        s=self.spool();s.establish(100,SCHEMA);self.assertTrue(s.append(*tx));self.assertFalse(s.append(*tx));s.close()
        s=self.spool();self.assertEqual(s.captured,210);self.assertEqual(list(s.transactions(100)),[(210,tx[2])]);s.close()
    def test_crash_before_and_after_durable_commit(self):
        for point,expected in [('before_spool_commit',100),('after_spool_commit',210)]:
            with self.subTest(point=point):
                root=self.root/point
                code='from capture.spool import Spool; import sys; s=Spool(sys.argv[1],{"id":1}); s.establish(100,{}); s.append(200,210,b"complete")'
                env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),SUPABRICKS_CAPTURE_FAILPOINT=point)
                self.assertEqual(subprocess.run([sys.executable,'-c',code,str(root)],env=env).returncode,86)
                s=Spool(root,{'id':1});self.assertEqual(s.captured,expected);s.verify();s.close()
    def test_ack_crash_boundaries_never_advance_beyond_spool(self):
        for point,has_feedback in [('before_source_ack',False),('after_source_ack',True)]:
            with self.subTest(point=point):
                a,b=socket.socketpair()
                code='from capture.protocol import Wire; import socket,sys; w=Wire.__new__(Wire); w.socket=socket.socket(fileno=int(sys.argv[1])); w.feedback(210)'
                env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),SUPABRICKS_CAPTURE_FAILPOINT=point)
                self.assertEqual(subprocess.run([sys.executable,'-c',code,str(b.fileno())],pass_fds=(b.fileno(),),env=env).returncode,86)
                b.close();wire=a.recv(100);a.close();self.assertEqual(bool(wire),has_feedback)
                if wire:self.assertEqual(struct.unpack('!QQQ',wire[6:30]),(210,210,0))
    def test_corrupt_payload_and_missing_history_fail_closed(self):
        s=self.spool();s.establish(100,SCHEMA);s.append(200,210,b'one');s.append(300,310,b'two');s.close()
        with sqlite3.connect(self.root/'spool.sqlite3') as db:db.execute('DELETE FROM transactions WHERE end_lsn=?',('00000000000000d2',))
        with self.assertRaises(CaptureError):self.spool()
        # Failed open must release the writer lock for cleanup/recovery tooling.
        with self.assertRaises(CaptureError) as e:self.spool()
        self.assertNotEqual(e.exception.code,'spool_already_owned')
    def test_identity_writer_and_replay_fences(self):
        s=self.spool();s.establish(100,SCHEMA);s.append(200,210,b'one')
        with self.assertRaises(CaptureError):self.spool()
        with self.assertRaises(CaptureError):s.append(200,210,b'changed')
        with self.assertRaises(CaptureError):s.append(150,220,b'out-of-order')
        s.close()
        with self.assertRaises(CaptureError):Spool(self.root,dict(IDENTITY,timeline_id='b'))
    def test_pressure_and_enospc_leave_cursor_unchanged(self):
        s=self.spool();s.establish(100,SCHEMA)
        with patch('capture.spool.os.statvfs') as space:
            space.return_value.f_bavail=0;space.return_value.f_frsize=4096
            with self.assertRaises(CaptureError):s.append(200,210,b'value')
        self.assertEqual(s.captured,100)
        s.limit=32768
        with self.assertRaises(CaptureError):s.append(200,210,b'value')
        self.assertEqual(s.captured,100);s.close()
    def test_spool_quota_is_bounded_and_last_commit_remains_recoverable(self):
        s=self.spool();s.establish(100,SCHEMA);payload=b'x'*(1024*1024)
        end=100
        with self.assertRaises(CaptureError):
            for _ in range(100):
                s.append(end+1,end+2,payload);end+=2
        self.assertGreater(end,100)
        self.assertEqual(s.captured,end)
        self.assertLessEqual(s.path.stat().st_size,16*1024*1024)
        s.close();s=self.spool();self.assertEqual(s.captured,end);s.close()

    def test_decoder_schema_message_and_transaction_limits(self):
        for payload in [b'X',b'T'+b'\0'*20,begin()+b'extra',b'I'+b'\0'*10]:
            with self.assertRaises(CaptureError):Decoder(SCHEMA,'fence').feed(payload)
        d=Decoder(SCHEMA,'fence');d.feed(begin());d.feed(relation())
        with self.assertRaises(CaptureError):d.feed(b'M'+struct.pack('!BQ',1,150)+b'fence\0'+struct.pack('!I',0))
        d=Decoder(SCHEMA,'fence');d.feed(begin());d.feed(relation())
        with self.assertRaises(CaptureError):
            for _ in range(8):d.feed(insert(b'a'*(MAX_MESSAGE-32)))
    def test_wire_length_rejected_before_payload_allocation(self):
        a,b=socket.socketpair();w=Wire.__new__(Wire);w.socket=a
        b.sendall(b'd'+struct.pack('!I',MAX_MESSAGE+100))
        with self.assertRaises(CaptureError):w.packet()
        a.close();b.close()
    def test_baseline_identity_schema_and_file_checksum(self):
        from capture.bootstrap import verify
        identity=dict(IDENTITY,project_id='p',branch_id='b',tenant_id='t')
        s=Spool(self.root,identity);s.establish(100,SCHEMA);s.set('profile',dict(database_oid=5))
        root=Path(self.temp.name)/'baseline';root.mkdir()
        data=b'private baseline';(root/'data').write_bytes(data)
        manifest=dict(format_version=1,status='files_complete',published=False,id='export',database='postgres',database_oid=5,
            source=dict(project_id='p',branch_id='b',tenant_id='t',timeline_id='a',export_branch_id='child',export_timeline_id='childtimeline',lsn='0/C8'),
            tables=[dict(oid=42,schema='public',name='orders',columns=[dict(name='id',type_oid=23,typmod=-1),dict(name='value',type_oid=25,typmod=-1)])],
            files=[dict(path='data',bytes=len(data),sha256=hashlib.sha256(data).hexdigest())])
        path=root/'manifest.json';path.write_text(json.dumps(manifest))
        config=dict(identity=identity,bootstrap=dict(id='export',manifest=str(path)))
        self.assertEqual(list(verify(config,s))[-1],'0/C8')
        alias=Path(self.temp.name)/'baseline-alias'
        alias.symlink_to(root,target_is_directory=True)
        aliased=dict(config,bootstrap=dict(id='export',manifest=str(alias/'manifest.json')))
        self.assertEqual(list(verify(aliased,s))[-1],'0/C8')
        (root/'data').write_bytes(b'x'*len(data))
        with self.assertRaises(CaptureError):list(verify(config,s))
        (root/'data').write_bytes(data);manifest['source']['lsn']='0/50';path.write_text(json.dumps(manifest))
        with self.assertRaises(CaptureError):list(verify(config,s))
        s.close()

    def test_private_paths(self):
        self.root.mkdir(mode=0o700);(self.root/'spool.sqlite3').symlink_to(self.root/'other')
        with self.assertRaisesRegex(CaptureError,'unsafe_spool_path'):self.spool()

    def test_status_replacement_recovers_a_longer_crash_temporary(self):
        from capture.spool import atomic
        root=Path(self.temp.name)
        temporary=root/'status.tmp'
        temporary.write_bytes(b'x'*8192);temporary.chmod(0o600)
        atomic(root/'status.json',dict(state='paused'))
        self.assertEqual(json.loads((root/'status.json').read_bytes()),dict(state='paused'))

if __name__=='__main__':unittest.main()
