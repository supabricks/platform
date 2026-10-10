"""Run native analytical modules in fresh interpreters with bounded diagnostics.

RSS high-water marks survive collection. Sharing one process makes later mailbox
lifecycle tests inherit earlier large-data tests' retirement conditions.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

CHILD = '''import faulthandler,sys,unittest
faulthandler.enable()
faulthandler.dump_traceback_later(30,repeat=True)
suite=unittest.defaultTestLoader.discover(sys.argv[1],pattern=sys.argv[2])
if not suite.countTestCases():raise SystemExit('module discovered no tests')
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
'''


def run(directory,pattern='test_*.py',timeout=180):
    modules=sorted(directory.glob(pattern))
    if not modules:raise ValueError('no test modules selected')
    results=[]
    for module in modules:
        print('MODULE',module.name,flush=True);started=time.monotonic()
        process=subprocess.Popen([sys.executable,'-B','-c',CHILD,str(directory),module.name],start_new_session=True)
        timed_out=False
        try:
            code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True;code=124
        finally:
            # Kill only this fixture's group, including leaked children on error.
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            process.wait()
        results.append(dict(module=module.name,exit_code=code,timed_out=timed_out,seconds=time.monotonic()-started))
        print(json.dumps(results[-1]),flush=True)
    return results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern',default='test_*.py')
    parser.add_argument('--timeout',type=float,default=180)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.timeout<=0:parser.error('timeout must be positive')
    results=run(Path(__file__).resolve().parent,args.pattern,args.timeout)
    if args.report:args.report.write_text(json.dumps(results,indent=2)+'\n')
    raise SystemExit(any(r['exit_code'] for r in results))
