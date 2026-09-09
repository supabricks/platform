#!/usr/bin/env python3
"""Record the unmodified Datalayer package's compatibility with console tooling."""
import argparse
import json
from pathlib import Path
import re
import subprocess

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--report',type=Path,required=True)
args=parser.parse_args()
root=Path(__file__).resolve().parent/'candidates/datalayer'
subprocess.run(['npm','ci','--prefix',str(root),'--no-audit','--no-fund'],check=True)
result=subprocess.run(['npm','run','build','--prefix',str(root)],capture_output=True,text=True)
log=re.sub(r'\x1b\[[0-9;]*m','',result.stdout+result.stderr)
expected='service-worker.js?text' in log and 'MISSING_EXPORT' in log
report=dict(candidate='@datalayer/jupyter-react',version='2.0.14',build_exit=result.returncode,
    decision='reject-unmodified-candidate' if result.returncode and expected else 'requires-review',
    node=subprocess.check_output(['node','--version'],text=True).strip(),
    locked_packages=len(json.loads((root/'package-lock.json').read_text())['packages'])-1,
    reason='Notebook entry imports a JupyterLite service-worker ?text module unsupported by the existing Vite configuration; no overrides applied.')
args.report.parent.mkdir(parents=True,exist_ok=True)
args.report.write_text(json.dumps(report,indent=2)+'\n')
args.report.with_suffix('.log').write_text(log)
assert expected and result.returncode, 'Candidate behavior changed; review selection rather than silently accepting it'
