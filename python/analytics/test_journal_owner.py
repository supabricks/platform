"""SP10b authority, bounded transport, cancellation and read-only recovery."""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from capture import owner
from capture.journal import ReadBusy
from capture.spool import Spool, CaptureError, atomic, canonical, pg_lsn
from incremental import storage


class PrivateJsonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'control.json';atomic(self.path,{'revision':1})

    def test_replacement_before_or_after_read_uses_current_version(self):
        for replace_at in (1,2):
            with self.subTest(replace_at=replace_at):
                atomic(self.path,{'revision':1});calls=[];original=os.fstat
                def replace(fd):
                    calls.append(fd)
                    if len(calls)==replace_at:atomic(self.path,{'revision':2})
                    return original(fd)
                with patch.object(owner.os,'fstat',replace):
                    self.assertEqual(owner.private_json(self.path,65536),{'revision':2})

    def test_replacement_after_descriptor_check_revalidates_path_identity(self):
        original=Path.lstat;changed=[]
        def replace(path,*args,**kwargs):
            if path==self.path and not changed:
                changed.append(True);atomic(path,{'revision':2})
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',replace):
            self.assertEqual(owner.private_json(self.path,65536),{'revision':2})

    def test_path_lookup_retired_before_stat_does_not_supply_authority(self):
        original=Path.lstat;changed=[]
        def replace(path,*args,**kwargs):
            meta=original(path,*args,**kwargs)
            if path==self.path and not changed:
                changed.append(True);atomic(path,{'revision':2})
                # A path lookup can race unlink before the kernel fills stat.
                fields=list(meta);fields[3]=0;return os.stat_result(fields)
            return meta
        with patch.object(Path,'lstat',replace):
            self.assertEqual(owner.private_json(self.path,65536),{'revision':2})

    def test_continuous_replacement_exhausts_bounded_read_only_attempts(self):
        original=os.fstat;calls=[]
        def replace(fd):
            if original(fd).st_ino==self.path.stat().st_ino:
                calls.append(fd);atomic(self.path,{'revision':len(calls)+1})
            return original(fd)
        with patch.object(owner.os,'fstat',replace),self.assertRaises(ReadBusy):
            owner.private_json(self.path,65536)
        self.assertEqual(len(calls),owner.PRIVATE_READ_ATTEMPTS)
        self.assertEqual(owner.private_json(self.path,65536),{'revision':len(calls)+1})

    def test_retry_respects_original_request_check_and_startup_deadline(self):
        original=os.fstat;calls=[]
        def replace(fd):
            if original(fd).st_ino==self.path.stat().st_ino:
                calls.append(fd);atomic(self.path,{'revision':2})
            return original(fd)
        with patch.object(owner.os,'fstat',replace):
            with self.assertRaisesRegex(CaptureError,'journal_read_cancelled'):
                owner.private_json(self.path,65536,check=Mock(
                    side_effect=[None,CaptureError('journal_read_cancelled')]))
            self.assertEqual(len(calls),1)
            with patch.object(owner.time,'monotonic',side_effect=[0,0,owner.READ_SECONDS]),self.assertRaises(ReadBusy):
                owner.private_json(self.path,65536)
            self.assertEqual(len(calls),2)

    def test_unsafe_files_remain_fatal(self):
        for kind in ('hardlink','symlink','public','foreign','oversize'):
            with self.subTest(kind=kind):
                self.path.unlink();atomic(self.path,{'revision':1})
                if kind=='hardlink':os.link(self.path,self.path.with_suffix('.link'))
                if kind=='symlink':
                    target=self.path.with_suffix('.target');self.path.rename(target);self.path.symlink_to(target)
                if kind=='public':self.path.chmod(0o644)
                if kind=='foreign':
                    with patch.object(owner.os,'getuid',return_value=os.getuid()+1),self.assertRaisesRegex(CaptureError,'unsafe_journal_owner_path'):
                        owner.private_json(self.path,65536)
                elif kind=='symlink':
                    with self.assertRaises(OSError):owner.private_json(self.path,65536)
                else:
                    with self.assertRaisesRegex(CaptureError,'unsafe_journal_owner_path'):
                        owner.private_json(self.path,1 if kind=='oversize' else 65536)


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='sb-ipc-',dir='/tmp')
        self.root=Path(self.temp.name)
        self.capture=str(uuid.uuid4());self.run=str(uuid.uuid4())
        for relative in ('tmp','capture',f'capture/{self.capture}','analytics','analytics/apply-work',f'analytics/apply-work/{self.run}'):
            (self.root/relative).mkdir(mode=0o700)
        self.control=self.root/'capture'/self.capture/'control.json'
        self.identity=dict(project_id='project-a',generation=self.capture,decoder_version=1,service_authority={'revision':1})
        self.endpoint=self.root/'tmp'/self.capture/'journal.sock'
        self.access=dict(endpoint=str(self.endpoint),source_revision=1,policy_revision=1)
        self.control_value=dict(identity=self.identity,worker_generation=1,desired='running',journal_access=self.access)
        atomic(self.control,self.control_value)
        self.spool=Spool(self.control.parent/'spool',self.identity)
        self.spool.establish(100,{});self.spool.set('bootstrap',{'lsn':'0/64'})
        self.spool.append_many([(180,200,b'first'),(280,300,b'second')])
        self.config=dict(id=self.run,attempt=1,epoch_id=str(uuid.uuid4()),identity=self.identity,
            worker_generation=1,bootstrap_lsn='0/64',after_lsn='0/64',target_lsn='0/12C',
            deadline_ms=time.time()*1000+60000,journal_access=self.access,spool=str(self.spool.path))
        self.input=self.root/'analytics'/'apply-work'/self.run/'input.json'
        atomic(self.input,self.config);self.server=owner.Owner(self.control,self.spool.backend)

    def tearDown(self):
        if self.server:self.server.close()
        self.spool.close();self.temp.cleanup()

    def request(self, config=None, seconds=3):
        return owner.read_range(owner.request_for(config or self.config),time.monotonic()+seconds)

    def connect(self, deadline=None):
        connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);connection.settimeout(4)
        connection.connect(str(self.endpoint))
        data=canonical(dict(version=1,request=owner.request_for(self.config),deadline=deadline or time.monotonic()+3))
        connection.sendall(struct.pack('!I',len(data))+data)
        return connection

    def assert_unpinned(self):
        fd=os.open(self.spool.root/'readers.lock',os.O_RDWR)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(fd)

    def test_parity_uses_owner_and_returns_transport_accounting(self):
        direct=storage.journal_attempt(self.config,time.monotonic()+3)
        with patch.object(storage,'snapshot',side_effect=AssertionError('client opened SQLite')):
            actual=storage.journal(self.config)
        self.assertEqual(actual,direct)
        stats=self.config['_journal_read']
        self.assertEqual(stats['outcome'],'complete');self.assertEqual(stats['attempts'],1)
        self.assertEqual(set(stats),{'attempts','busy','wait_ms','elapsed_ms','outcome'})
        transport={}
        self.assertEqual(owner.read_range(owner.request_for(self.config),time.monotonic()+3,transport),actual)
        self.assertGreater(transport['response_bytes'],actual[-1]);self.assertGreater(transport['owner_read_ms'],0)
        self.assert_unpinned()

    def test_exact_request_and_project_generation_authority_fences(self):
        mutations=[('id',str(uuid.uuid4())),('attempt',2),('epoch_id',str(uuid.uuid4())),
            ('identity',dict(self.identity,project_id='other')),('identity',dict(self.identity,service_authority={'revision':2})),
            ('worker_generation',2),('bootstrap_lsn','0/65'),('after_lsn','0/C8'),('target_lsn','0/190'),
            ('deadline_ms',self.config['deadline_ms']+1000),('journal_access',dict(self.access,policy_revision=2)),
            ('journal_access',dict(self.access,source_revision=2))]
        for key,value in mutations:
            with self.subTest(key=key,value=value):
                config=copy.deepcopy(self.config);config[key]=value
                with self.assertRaisesRegex(CaptureError,'journal_owner_fenced'):self.request(config)
        self.assertEqual(self.request()[2],300)

    def test_atomic_replacement_retries_current_authority_and_fences_revocations(self):
        original=os.fstat
        for change in ('same','pause','generation','policy','source','grant'):
            with self.subTest(change=change):
                atomic(self.control,self.control_value);atomic(self.input,self.config)
                target=self.input if change=='grant' else self.control
                inode=target.stat().st_ino;replaced=[]
                def replace(fd):
                    if original(fd).st_ino==inode and not replaced:
                        replaced.append(True);value=copy.deepcopy(self.control_value)
                        if change=='pause':value['desired']='paused'
                        if change=='generation':value['worker_generation']=2
                        if change=='policy':value['journal_access']['policy_revision']=2
                        if change=='source':value['journal_access']['source_revision']=2
                        if change=='grant':value=dict(self.config,attempt=2)
                        atomic(target,value)
                    return original(fd)
                with patch.object(owner.os,'fstat',replace):
                    if change=='same':self.assertEqual(self.request()[2],300)
                    else:
                        with self.assertRaisesRegex(CaptureError,'journal_owner_fenced'):self.request()
                self.assertTrue(replaced);self.assert_unpinned();self.server.check()

    def test_replacement_churn_after_payload_defers_without_completion(self):
        original=os.fstat;send=owner.Channel.send;churn=[];replacements=[]
        def begin(channel,data):
            send(channel,data)
            if isinstance(data,bytearray):churn.append(True)
        def replace(fd):
            if churn and original(fd).st_ino==self.control.stat().st_ino:
                replacements.append(fd);atomic(self.control,self.control_value)
            return original(fd)
        with patch.object(owner.Channel,'send',begin),patch.object(owner.os,'fstat',replace),self.assertRaises(ReadBusy):
            self.request()
        self.assertEqual(len(replacements),owner.PRIVATE_READ_ATTEMPTS)
        self.assert_unpinned();self.server.check()
        self.assertEqual(self.request()[2],300)

    def test_replacement_churn_before_snapshot_is_retryable_and_owner_recovers(self):
        original=os.fstat;replacements=[]
        def replace(fd):
            if original(fd).st_ino==self.control.stat().st_ino:
                replacements.append(fd);atomic(self.control,self.control_value)
            return original(fd)
        with patch.object(owner.os,'fstat',replace),patch.object(self.spool.backend,'snapshot') as snapshot:
            with self.assertRaises(ReadBusy):self.request()
            snapshot.assert_not_called()
        self.assertEqual(len(replacements),owner.PRIVATE_READ_ATTEMPTS)
        self.assert_unpinned();self.server.check();self.assertEqual(self.request()[2],300)

    def test_startup_retries_atomic_control_replacement(self):
        self.server.close();self.server=None;original=os.fstat;replaced=[]
        def replace(fd):
            if not replaced:
                replaced.append(True);atomic(self.control,self.control_value)
            return original(fd)
        with patch.object(owner.os,'fstat',replace):self.server=owner.Owner(self.control,self.spool.backend)
        self.assertTrue(replaced);self.assertEqual(self.request()[2],300)

    def test_current_control_revocation_and_retry_replacement_fence_before_response(self):
        original=owner.materialize
        for change in ('pause','generation','policy','attempt','remove'):
            with self.subTest(change=change):
                atomic(self.control,self.control_value);atomic(self.input,self.config)
                def revoke(*args,**kwargs):
                    result=original(*args,**kwargs)
                    control=copy.deepcopy(self.control_value)
                    if change=='pause':control['desired']='paused'
                    if change=='generation':control['worker_generation']=2
                    if change=='policy':control['journal_access']['policy_revision']=2
                    if change=='attempt':atomic(self.input,dict(self.config,attempt=2))
                    elif change=='remove':self.input.unlink()
                    else:atomic(self.control,control)
                    return result
                with patch.object(owner,'materialize',revoke),self.assertRaisesRegex(CaptureError,'journal_owner_fenced'):
                    self.request()
                self.assert_unpinned()
        atomic(self.control,self.control_value);atomic(self.input,self.config)
        self.assertEqual(self.request()[2],300)

    def test_backend_corruption_is_fatal_and_no_partial_rows_escape(self):
        self.spool.backend.db.execute("UPDATE transactions SET payload=x'626164' WHERE end_lsn='000000000000012c'")
        with self.assertRaisesRegex(CaptureError,'spool_corrupt'):self.request()
        self.assert_unpinned();self.server.check()

    def test_oversize_request_rejected_before_body_and_owner_remains_usable(self):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.connect(str(self.endpoint));connection.sendall(struct.pack('!I',owner.MAX_REQUEST+1))
            channel=owner.Channel(connection,time.monotonic()+1)
            self.assertEqual(json.loads(channel.frame(owner.MAX_HEADER))['error'],'journal_owner_budget')
        self.assertEqual(self.request()[2],300)

    def test_missing_owner_defers_without_direct_database_fallback(self):
        self.server.close();self.server=None
        with patch.object(storage,'snapshot',side_effect=AssertionError('fallback')),patch.object(storage,'JOURNAL_READ_SECONDS',.12):
            with self.assertRaises(storage.JournalBusyDeferred):storage.journal(self.config)
        self.assertEqual(self.config['_journal_read']['outcome'],'deferred')
        self.assertGreater(self.config['_journal_read']['busy'],0)
        self.assertEqual(self.spool.captured,300)

    def test_disconnect_cancels_snapshot_and_releases_lease(self):
        pinned=threading.Event();released=threading.Event();original=self.spool.backend.snapshot
        @contextmanager
        def hold(deadline,busy,cancelled):
            with original(deadline,busy,cancelled) as view:
                view.metadata();pinned.set()
                while not cancelled() and time.monotonic()<deadline:time.sleep(.005)
                try:yield view
                finally:released.set()
        with patch.object(self.spool.backend,'snapshot',hold):
            connection=self.connect();self.assertTrue(pinned.wait(1));connection.close()
            self.assertTrue(released.wait(1))
        # released is set just before the context closes; a following accepted
        # request proves the service loop has finalized that context.
        self.assertEqual(self.request()[2],300);self.assert_unpinned()

    def test_slow_reader_holds_no_snapshot_and_shutdown_is_bounded(self):
        self.spool.append_many([(380,400,b'x'*(2*1024*1024)),(480,500,b'y'*(2*1024*1024))])
        self.config['target_lsn']='0/1F4';atomic(self.input,self.config)
        connection=self.connect()
        try:
            channel=owner.Channel(connection,time.monotonic()+3);header=json.loads(channel.frame(owner.MAX_HEADER))
            self.assertEqual(header['end'],500);self.assert_unpinned()
            self.spool.append(580,600,b'writer continues')
            self.assertEqual(self.spool.captured,600)
            start=time.monotonic();self.server.close();self.server=None
            self.assertLess(time.monotonic()-start,1)
        finally:connection.close()

    def test_bounded_materialization_stops_at_record_and_payload_limit(self):
        with patch.object(owner,'MAX_RECORDS',1):
            self.assertEqual(self.request()[1:],([(200,b'first')],200,5))
        self.spool.append_many([(380,400,b'x'*(4*1024*1024))])
        for n in range(5,9):self.spool.append(n*100-20,n*100,b'x'*(4*1024*1024))
        self.config['target_lsn']=pg_lsn(800);atomic(self.input,self.config)
        result=self.request();self.assertLessEqual(result[3],owner.MAX_BATCH)
        self.assertLess(result[2],800);self.assert_unpinned()

    def test_expired_queued_request_never_opens_snapshot(self):
        with patch.object(self.spool.backend,'snapshot',side_effect=AssertionError('expired read')):
            connection=self.connect(deadline=time.monotonic()-.01)
            try:self.assertEqual(connection.recv(1),b'')
            finally:connection.close()
        self.assertEqual(self.request()[2],300)

    def test_queue_pressure_shares_deadline_and_expired_readers_open_no_snapshot(self):
        from concurrent.futures import ThreadPoolExecutor
        self.spool.append(380,400,b'x'*(4*1024*1024))
        self.config['target_lsn']='0/190';atomic(self.input,self.config)
        original=self.spool.backend.snapshot
        with patch.object(self.spool.backend,'snapshot',wraps=original) as snapshots:
            connection=self.connect()
            try:
                channel=owner.Channel(connection,time.monotonic()+3)
                self.assertEqual(json.loads(channel.frame(owner.MAX_HEADER))['end'],400)
                def queued(_):
                    started=time.monotonic()
                    with self.assertRaises(ReadBusy):self.request(seconds=.2)
                    return time.monotonic()-started
                with ThreadPoolExecutor(max_workers=8) as pool:elapsed=list(pool.map(queued,range(8)))
                self.assertLess(max(elapsed),.6);self.assertEqual(snapshots.call_count,1)
                self.assert_unpinned()
            finally:connection.close()
            # A just-closed backlog can still reject the first connect with
            # EAGAIN. Exercise production's bounded read-only retry policy.
            self.assertEqual(storage.journal(self.config)[2],400)
            self.assertEqual(snapshots.call_count,2)

    def test_symlink_grant_and_non_socket_endpoint_are_rejected(self):
        original=self.input.read_bytes();self.input.unlink();self.input.symlink_to(self.control)
        with self.assertRaisesRegex(CaptureError,'journal_owner_fenced'):self.request()
        self.input.unlink();self.input.write_bytes(original);self.input.chmod(0o600)
        self.server.close();self.server=None;self.endpoint.touch(mode=0o600)
        with self.assertRaisesRegex(CaptureError,'unsafe_journal_owner_path'):
            owner.Owner(self.control,self.spool.backend)
        self.assertTrue(self.endpoint.is_file())

    def test_revocation_after_payload_requires_fenced_completion_marker(self):
        send=owner.Channel.send
        def revoke(channel,data):
            send(channel,data)
            if isinstance(data,bytearray):
                atomic(self.control,dict(self.control_value,desired='paused'))
        with patch.object(owner.Channel,'send',revoke),self.assertRaises(ReadBusy):self.request()
        # Complete bytes without DONE never reach apply. The next attempt sees
        # the explicit authority fence rather than accepting a partial result.
        with self.assertRaisesRegex(CaptureError,'journal_owner_fenced'):self.request()

    def test_process_kill_reclaims_stale_socket_and_fences_previous_generation(self):
        self.server.close();self.server=None;self.spool.close()
        code="""
import json,sys,time
from pathlib import Path
from capture.spool import Spool
from capture.owner import Owner
control=Path(sys.argv[1]);config=json.loads(control.read_bytes())
spool=Spool(control.parent/'spool',config['identity']);owner=Owner(control,spool.backend)
while True:time.sleep(1)
"""
        process=subprocess.Popen([sys.executable,'-c',code,str(self.control)],
            env=dict(os.environ,PYTHONPATH=str(Path(__file__).parent)))
        try:
            deadline=time.monotonic()+5
            while not self.endpoint.exists() and time.monotonic()<deadline and process.poll() is None:time.sleep(.01)
            self.assertEqual(self.request()[2],300)
        finally:process.kill();process.wait(timeout=5)
        self.assertTrue(self.endpoint.exists())
        self.spool=Spool(self.control.parent/'spool',self.identity);self.spool.verify()
        atomic(self.control,dict(self.control_value,worker_generation=2))
        self.server=owner.Owner(self.control,self.spool.backend)
        with self.assertRaisesRegex(CaptureError,'journal_owner_fenced'):self.request()
        self.config['worker_generation']=2;atomic(self.input,self.config)
        self.assertEqual(self.request()[1],[(200,b'first'),(300,b'second')])

    def test_client_rejects_response_budget_corruption_and_truncation(self):
        self.server.close();self.server=None
        for case in ('budget','checksum','truncated','fence'):
            with self.subTest(case=case):
                listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(str(self.endpoint));self.endpoint.chmod(0o600);listener.listen(1)
                def malicious():
                    with listener.accept()[0] as connection:
                        channel=owner.Channel(connection,time.monotonic()+2);request=json.loads(channel.frame(owner.MAX_REQUEST))['request']
                        if case=='budget':channel.send(struct.pack('!I',owner.MAX_HEADER+1));return
                        header=dict(version=1,request_sha256=hashlib.sha256(canonical(request)).hexdigest(),schema={},count=1,end=200,used=5)
                        if case=='fence':header['request_sha256']='different'
                        data=canonical(header);channel.send(struct.pack('!I',len(data))+data)
                        if case=='truncated':return
                        if case=='fence':return
                        channel.send(owner.RECORD.pack(200,5,b'x'*32)+b'first'+b'DONE')
                thread=threading.Thread(target=malicious);thread.start()
                try:
                    expected=ReadBusy if case=='truncated' else CaptureError
                    with self.assertRaises(expected):self.request()
                finally:thread.join(3);listener.close();self.endpoint.unlink()


if __name__=='__main__':unittest.main()
