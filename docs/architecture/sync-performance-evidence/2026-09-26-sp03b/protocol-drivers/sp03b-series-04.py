import subprocess
from pathlib import Path
harness=Path('/tmp/sp03b-harness-04')
subprocess.run(['python3','/tmp/sp03b-screen-04.py'],check=True)
revision='5382e80032bb4fc70706f112db26f8415849a961';previous='da548e7b7564d554018a9b34281e9e42320028ee'
pred='/tmp/sp03b-predecessor-01';candidate='/tmp/sp03b-runtime-03'
common=['python3',str(harness/'e2e/native/performance/compare.py'),'--predecessor-harness',str(harness),'--candidate-harness',str(harness)]
commands=[]
for label,package,rev in [('predecessor',pred,previous),('candidate',candidate,revision)]:
 commands.append(common+['--slice','SP03b-'+label+'-controls','--hypothesis','Same unchanged runtime instrumentation off/on; common checkpoint probe activation control.','--activation-control','--cells','4:50,8:1000,16:1000','--predecessor-release',package,'--candidate-release',package,'--predecessor-revision',rev,'--candidate-revision',rev,'--output','/tmp/sp03b-'+label+'-controls-01'])
commands.append(common+['--slice','SP03b','--hypothesis','Capture-only WAL/FULL with bounded checkpoints improves durable capture reader concurrency over SP03a batching; no source capacity claim.','--predecessor-release',pred,'--candidate-release',candidate,'--predecessor-revision',previous,'--candidate-revision',revision,'--baseline',str(harness/'docs/architecture/sync-performance-evidence/2026-09-24-workflow-profile'),'--output','/tmp/sp03b-comparison-01'])
for mode,grouped in [('delete','/tmp/sp03b-delete-03'),('wal',candidate)]:
 commands.append(common+['--slice','SP03b-'+mode+'-ablation','--hypothesis','Frozen SP03b implementation: isolate grouping interaction within '+mode+' journal mode.','--cells','16:1000','--predecessor-release','/tmp/sp03b-single-'+mode+'-03','--candidate-release',grouped,'--predecessor-revision',revision,'--candidate-revision',revision,'--output','/tmp/sp03b-'+mode+'-ablation-01'])
for command in commands:
 print('START',command[-1],flush=True);subprocess.run(command,cwd=harness,check=True);print('COMPLETE',command[-1],flush=True)
print('SP03B_SERIES_COMPLETE',flush=True)
