#!/usr/bin/env python3
"""SP09a lifecycle qualification: reused epochs, in-flight kill, idle retirement."""
import json
import os
from pathlib import Path
import signal
import time
from dispatch_component import DispatchProbe, continuous
from cell import wait


class ReuseProbe(DispatchProbe):
    def commit_markers(self,cap):
        if not self.marker_enabled:return super().commit_markers(cap)
        from marker_observer import rows
        from types import SimpleNamespace
        reader=self.marker_readers.setdefault(cap['id'],SimpleNamespace(
            spool=self.root/'capture'/cap['id']/'spool/spool.sqlite3',capture_id=cap['id'],seq=0))
        import struct
        result=rows(reader)
        if result:reader.seq=result[-1][0]
        return [(struct.unpack('!I',xid)[0],int(end,16)) for _,end,xid in result]

    def workload(self,cap):
        super().workload(cap)
        # The common predecessor intentionally has no reusable worker protocol.
        if not (Path(self.worker_path).parent/'incremental/reuse.py').exists():return
        def workers():return [r for r in self.records() if r['role'].startswith('incremental-')]
        pids=[]
        for value in (921,922,923):
            self.sql(self.parent,f'UPDATE orders SET value={value} WHERE id=9801; UPDATE payments SET value={value} WHERE id=9801')
            self.wait_value(9801,value)
            live=workers();assert len(live)==1,live
            pids.append(live[0]['pid'])
        assert len(set(pids))==1,pids
        self.check('successive_epochs_reuse_one_owned_process')
        process=workers()[0];os.kill(process['pid'],signal.SIGSTOP)
        previous=self.current()['epoch_id']
        try:
            self.sql(self.parent,'UPDATE orders SET value=924 WHERE id=9802; UPDATE payments SET value=924 WHERE id=9802')
            wait(lambda:any(r['state']=='running' and r['apply_id'] for r in self.sync('runs',id=self.policy_id)))
            assert self.current()['epoch_id']==previous
        finally:os.kill(process['pid'],signal.SIGKILL)
        self.wait_value(9802,924)
        self.check('inflight_reused_worker_sigkill_recovers_atomic_group')
        wait(lambda:not workers(),timeout=15)
        epoch=self.current()['epoch_id'];time.sleep(1)
        assert self.current()['epoch_id']==epoch
        self.check('idle_workers_retire_without_empty_publications')
        self.metrics['worker_reuse_component']=dict(reused_pids=pids,killed_pid=process['pid'],idle_retired=True)

    def run(self,python,worker):
        from marker_observer import enable
        self.marker_enabled=enable(self,Path(worker).parent);self.marker_readers={}
        self.worker_path=worker
        return super().run(python,worker)


if __name__=='__main__':
    continuous.Continuous=ReuseProbe
    continuous.main()
