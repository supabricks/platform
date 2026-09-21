#!/usr/bin/env python3
"""Run one private fixture and account for its descendants, including detached UC."""
import argparse
import json
import subprocess
import time
from pathlib import Path
import psutil


def run(args):
    child=subprocess.Popen(args.command)
    owned={}
    def census():
        roots=[]
        try:roots.append(psutil.Process(child.pid))
        except psutil.NoSuchProcess:pass
        # The console CLI can detach its daemon between samples. Recover the
        # owner from this fixture's unique data-root prefix, never global PIDs.
        if args.data_root:
            for process in psutil.process_iter(['cmdline']):
                cmd=process.info['cmdline'] or []
                if 'daemon' not in cmd or '--data-dir' not in cmd:continue
                position=cmd.index('--data-dir')+1
                if position<len(cmd) and (cmd[position]==str(args.data_root) or cmd[position].startswith(str(args.data_root)+'/')):
                    roots.append(process)
        for root in roots:
            try:
                for p in [root,*root.children(recursive=True)]:
                    if p.pid!=child.pid:owned[(p.pid,p.create_time())]=p
            except psutil.NoSuchProcess:pass
    deadline=time.monotonic()+args.timeout
    timed_out=False
    while child.poll() is None:
        census()
        if time.monotonic()>deadline:
            timed_out=True;child.kill();break
        time.sleep(.1)
    code=child.wait()
    census()
    def alive(p):
        try:return p.is_running() and p.status()!=psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:return False
    remaining=[p for p in owned.values() if alive(p)]
    # Give normal process exit/reparenting a bounded opportunity to settle.
    if remaining:psutil.wait_procs(remaining,timeout=3)
    remaining=[p for p in remaining if alive(p)]
    leaked=len(remaining)
    for p in remaining:
        try:p.terminate()
        except psutil.NoSuchProcess:pass
    _,left=psutil.wait_procs(remaining,timeout=5)
    for p in left:
        try:p.kill()
        except psutil.NoSuchProcess:pass
    psutil.wait_procs(left,timeout=5)
    report=dict(exit_code=code,timed_out=timed_out,descendants_observed=len(owned),
                leaked_descendants=leaked,remaining_descendants=sum(alive(p) for p in left))
    args.report.write_text(json.dumps(report)+'\n')
    return 1 if code or timed_out or leaked else 0


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--timeout',type=int,default=1500)
    p.add_argument('--data-root',type=Path)
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('command',nargs=argparse.REMAINDER)
    args=p.parse_args()
    if args.command[:1]==['--']:args.command=args.command[1:]
    raise SystemExit(run(args))
