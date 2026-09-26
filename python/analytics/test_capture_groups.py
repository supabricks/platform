"""SP02 atomic groups, bounded accumulation, partial framing and crash authority."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from capture.groups import Groups
from capture.protocol import Wire, Decoder
from capture.spool import Spool, CaptureError, MAX_TRANSACTION, MAX_MESSAGE
from test_capture import SCHEMA, begin, commit, insert, relation


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'spool'
        self.spool=Spool(self.root,{'id':1});self.spool.establish(100,{})
    def tearDown(self):self.spool.close();self.tmp.cleanup()
    def txs(self,n=3,start=100):return [(start+i*100-20,start+i*100,b'complete'+str(i).encode()) for i in range(1,n+1)]

    def test_group_replay_and_new_suffix_are_atomic(self):
        txs=self.txs();self.spool.append_many(txs[:2])
        statements=[];self.spool.db.set_trace_callback(statements.append)
        result=self.spool.append_many(txs)
        self.assertEqual(result['transactions'],1);self.assertEqual(result['captured_lsn'],400)
        self.assertEqual(statements.count('COMMIT'),1)
        self.assertEqual(self.spool.append_many(txs)['transactions'],0)
        self.assertEqual(list(self.spool.transactions(100)),[(end,raw) for _,end,raw in txs])
        self.spool.verify()

    def test_invalid_late_member_does_not_commit_earlier_members(self):
        for txs in [[(180,200,b'one'),(150,300,b'bad')],[(180,200,b'one'),(180,200,b'one')]]:
            with self.assertRaisesRegex(CaptureError,'noncontiguous_commit_order'):self.spool.append_many(txs)
            self.assertEqual(self.spool.captured,100)
        self.spool.append_many(self.txs(1))
        with self.assertRaisesRegex(CaptureError,'replay_mismatch'):
            self.spool.append_many([(180,200,b'changed'),(280,300,b'new')])
        self.assertEqual(self.spool.captured,200)
        with self.assertRaisesRegex(CaptureError,'replay_mismatch'):self.spool.append_many([(179,200,b'complete1')])

    def test_identity_and_group_memory_bounds(self):
        for txs in [[],self.txs(129),[(180,200,b'x'*MAX_TRANSACTION),(280,300,b'y')]]:
            with self.assertRaisesRegex(CaptureError,'group_budget'):self.spool.append_many(txs)
        self.spool.set('identity',{'id':2})
        with self.assertRaisesRegex(CaptureError,'identity'):self.spool.append_many(self.txs())
        self.assertEqual(self.spool.captured,100)

    def test_whole_group_space_reservation_and_sqlite_full_rollback(self):
        with patch('capture.spool.os.statvfs') as space:
            space.return_value.f_bavail=0;space.return_value.f_frsize=4096
            with self.assertRaisesRegex(CaptureError,'spool_budget'):self.spool.append_many(self.txs())
        self.assertEqual(self.spool.captured,100)
        pages=self.spool.db.execute('PRAGMA page_count').fetchone()[0]
        self.spool.db.execute(f'PRAGMA max_page_count={pages+1}')
        with self.assertRaises(sqlite3.OperationalError):
            self.spool.append_many([(180,200,b'a'),(280,300,b'x'*262144)])
        self.assertEqual(self.spool.captured,100);self.assertEqual(list(self.spool.transactions(100)),[])
        self.spool.verify()

    def test_ambiguous_commit_is_reopened_verified_and_only_then_accepted(self):
        class Uncertain:
            def __init__(self,db,committed):self.db=db;self.committed=committed
            def __getattr__(self,name):return getattr(self.db,name)
            def execute(self,sql,*args):
                if sql=='COMMIT':
                    self.db.execute('COMMIT' if self.committed else 'ROLLBACK')
                    raise sqlite3.OperationalError('injected ambiguous I/O response')
                return self.db.execute(sql,*args)
        self.spool.db=Uncertain(self.spool.db,False)
        with self.assertRaises(sqlite3.OperationalError):self.spool.append_many(self.txs())
        self.assertEqual(self.spool.captured,100)
        self.spool.db=Uncertain(self.spool.db,True)
        self.assertEqual(self.spool.append_many(self.txs())['captured_lsn'],400)
        self.spool.verify()

    def test_count_age_bytes_and_oversize_are_separate_bounds(self):
        now=[10.0];g=Groups(self.spool,count=2,byte_limit=30,age=.010,clock=lambda:now[0])
        self.assertFalse(g.add((180,200,b'one')));self.assertAlmostEqual(g.wait(),.010)
        self.assertEqual(self.spool.captured,100)
        now[0]+=.011;self.assertEqual(g.wait(),0);g.flush();self.assertEqual(self.spool.captured,200)
        self.assertFalse(g.add((280,300,b'two')));self.assertTrue(g.add((380,400,b'three')));g.flush()
        self.assertTrue(g.add((480,500,b'x'*31)));self.assertTrue(g.before((580,600,b'y')))
        with self.assertRaisesRegex(CaptureError,'group_budget'):g.add((580,600,b'y'))
        g.flush();self.assertEqual(g.stats['groups'],3);self.assertEqual(g.stats['maximum_transactions'],2)
        immediate=Groups(self.spool,age=0);self.assertTrue(immediate.add((580,600,b'end')))
        self.assertEqual(g.progress()['durable_lsn'],500)

    def test_barrier_in_middle_of_atomic_group_survives_reopen(self):
        import uuid
        from test_triggered import message
        from test_incremental import tx
        identity=dict(generation=str(uuid.uuid4()),decoder_version=2);run=str(uuid.uuid4())
        self.spool.close();self.spool=Spool(Path(self.tmp.name)/'barriers',identity);self.spool.establish(100,{})
        rows=[(180,200,tx(180,200)),(280,300,tx(280,300,message('supabricks.barrier.'+identity['generation'],run))),(380,400,tx(380,400))]
        self.spool.append_many(rows);self.spool.verify()
        self.assertEqual(self.spool.get('barrier'),dict(run_id=run,end_lsn='0/12C'))
        g=Groups(self.spool);self.assertFalse(g.add(rows[0]));self.assertTrue(g.add(rows[1]))
        root=self.spool.root;self.spool.close();self.spool=Spool(root,identity)
        self.assertEqual(self.spool.captured,400);self.spool.verify()

    def test_pruned_reconnect_anchor_and_group_new_suffix(self):
        rows=[(101+2*i,102+2*i,b'x'*8192) for i in range(300)]
        for i in range(0,300,32):self.spool.append_many(rows[i:i+32])
        self.assertGreater(self.spool.prune('0/2BC'),0)
        self.assertEqual(self.spool.append_many([rows[-1],(701,702,b'new')])['transactions'],1)
        with self.assertRaisesRegex(CaptureError,'replay_mismatch'):self.spool.append_many([rows[0]])
        self.spool.verify()

    def test_process_death_at_group_and_feedback_boundaries(self):
        self.spool.close()
        for point,committed,ack in [('before_spool_group',False,False),('before_spool_commit',False,False),('after_spool_commit',True,False),('before_source_ack',True,False),('after_source_ack',True,True)]:
            with self.subTest(point=point):
                a,b=socket.socketpair();root=Path(self.tmp.name)/point
                code='''import socket,sys
from capture.spool import Spool
from capture.protocol import Wire
s=Spool(sys.argv[1],{'id':1});s.establish(100,{})
s.append_many([(180,200,b'one'),(280,300,b'two'),(380,400,b'three')])
w=Wire.__new__(Wire);w.socket=socket.socket(fileno=int(sys.argv[2]));w.feedback(s.captured)
'''
                env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent),SUPABRICKS_CAPTURE_FAILPOINT=point)
                result=subprocess.run([sys.executable,'-c',code,str(root),str(b.fileno())],env=env,pass_fds=(b.fileno(),))
                b.close();feedback=a.recv(100);a.close();self.assertEqual(result.returncode,86);self.assertEqual(bool(feedback),ack)
                s=Spool(root,{'id':1});self.assertEqual(s.captured,400 if committed else 100);s.verify()
                if feedback:self.assertEqual(struct.unpack('!QQQ',feedback[6:30]),(s.captured,s.captured,0))
                self.assertEqual(len(list(s.transactions(100))),3 if committed else 0);s.close()


class WorkerGroupTests(unittest.TestCase):
    def exercise(self, action):
        import capture_worker as worker
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'config.json'
            config=dict(identity={'id':1},worker_generation=1,desired='running',spool_bytes=16*1024*1024,socket_dir='/unused',port=123)
            path.write_text(json.dumps(config));events=[];clock=[10.0]
            def control(desired=None,fenced=False):
                value=dict(config)
                if desired:value['desired']=desired
                if fenced:value['worker_generation']=2
                path.write_text(json.dumps(value))
            class Source:
                def __init__(self,config,spool):self.spool=spool;self.slot='slot';self.publication='pub';self.fence='fence'
                def setup(self):self.spool.establish(100,{});return {'relations':{}}
                def check(self):return {'source':'0/190','retained_bytes':0}
                def cleanup(self):events.append(('cleanup',self.spool.captured))
                def close(self):pass
            class Decoder:
                def __init__(self,*args):pass
                def feed(self,data):return (180,200,b'complete')
            class Wire:
                def __init__(self,*args):self.step=0
                def start(self,*args):pass
                def feedback(self,captured,request=False):events.append(('feedback',captured))
                def close(self):
                    if action=='pause':control('deleted')
                def receive(self,timeout):
                    self.step+=1
                    if self.step==1:return ('data',999,b'tx')
                    if action=='fence':control(fenced=True)
                    elif action=='stop':signal.getsignal(signal.SIGTERM)(signal.SIGTERM,None)
                    else:control('paused')
                    return ('keepalive',999,1)
            with patch.object(worker,'Source',Source),patch.object(worker,'Wire',Wire),patch.object(worker,'Decoder',Decoder),patch.object(worker.time,'monotonic',lambda:clock[0]),patch.object(worker.time,'sleep',lambda _:None):
                result=worker.run(path)
            s=Spool(root/'spool',config['identity']);captured=s.captured;s.verify();s.close()
            return result,captured,events,json.loads((root/'status.json').read_text())

    def test_keepalive_acknowledges_only_durable_cursor_then_pause_flushes(self):
        result,captured,events,status=self.exercise('pause')
        self.assertIsNone(result);self.assertEqual(captured,200)
        self.assertEqual(events[:2],[('feedback',100),('feedback',200)])
        self.assertEqual(status['state'],'deleted')

    def test_orderly_signal_flushes_complete_group(self):
        result,captured,events,status=self.exercise('stop')
        self.assertIsNone(result);self.assertEqual(captured,200)
        self.assertEqual(events,[('feedback',100),('feedback',200)])
        self.assertEqual(status['state'],'paused')

    def test_fencing_discards_unacknowledged_pending_group(self):
        result,captured,events,status=self.exercise('fence')
        self.assertEqual(result,1);self.assertEqual(captured,100)
        self.assertEqual(events[0],('feedback',100));self.assertNotIn(('feedback',200),events)
        self.assertEqual(status['error'],'worker_fenced')


class StreamTests(unittest.TestCase):
    def test_partial_wire_packet_preserves_bytes_without_overriding_flush_deadline(self):
        a,b=socket.socketpair();self.addCleanup(a.close);self.addCleanup(b.close)
        w=Wire.__new__(Wire);w.socket=a;a.settimeout(3)
        data=b'k'+struct.pack('!QqB',400,0,1);packet=b'd'+struct.pack('!I',len(data)+4)+data
        b.sendall(packet[:7]);start=time.monotonic()
        self.assertIsNone(w.receive(timeout=.010));self.assertLess(time.monotonic()-start,.2)
        b.sendall(packet[7:]);self.assertEqual(w.receive(timeout=.010),('keepalive',400,1))
        b.sendall(b'd'+struct.pack('!I',MAX_MESSAGE+100))
        with self.assertRaisesRegex(CaptureError,'message_budget'):w.receive(timeout=.010)

    def test_partial_oversized_transaction_cannot_be_committed(self):
        d=Decoder(SCHEMA,'fence');d.feed(begin());d.feed(relation())
        with self.assertRaisesRegex(CaptureError,'transaction_budget'):
            for _ in range(8):self.assertIsNone(d.feed(insert(b'x'*(MAX_MESSAGE-32))))

if __name__=='__main__':unittest.main()
