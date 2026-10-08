"""Continue the frozen SP06 protocol after the independently launched source screen.

Stop on measurement/cleanup errors or exhausted replacement budgets. Never rerun
or overwrite a phase automatically. Observer concerns remain explicit review gates.
"""
from pathlib import Path
import fcntl,hashlib,json,os,subprocess,sys,time,traceback
root=Path(__file__).resolve().parent
repo=root.parent.parent
harness=root/'harness'
old=harness.parent/'sp04-harness'
release=root/'accepted-runtime'
revision='e10d5152f5681af7c233701e0fd75c100cbf4c6c'
image='sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec'
analysis=repo/'e2e/native/performance'
sys.path.insert(0,str(analysis))
from source_analysis import analyze
from archive_source import archive
state={'phase':'source-screen-02','status':'waiting_for_existing_controller','completed':[],
       'runtime_revision':revision,'image':image,'review_flags':[]}
lock=(root/'.continuation.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
def save():
    p=root/'campaign-status.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(state,indent=2)+'\n');tmp.replace(p)
def execute(command,phase):
    state.update(phase=phase,status='running');save();print('PHASE_START',phase,flush=True)
    with (root/(phase+'.log')).open('x') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,cwd=repo)
    if result.returncode:
        state.update(status='stopped_on_failure',exit_code=result.returncode);save()
        if (root/phase/'experiment.json').exists() and phase.startswith('source-'):
            archive(root/phase,root/'archives'/phase,allow_incomplete=True)
        raise RuntimeError('phase failed: '+phase)
    state['completed'].append(phase);save();print('PHASE_COMPLETE',phase,flush=True)
def source_phase(phase,mode,cpus,clients):
    command=[sys.executable,str(harness/'e2e/native/performance/source_capacity.py'),
             '--release',str(release),'--runtime-revision',revision,'--output',str(root/phase),
             '--mode',mode,'--cpus',*map(str,cpus),'--clients',*map(str,clients),
             '--repeats','3','--rate','10000','--seconds','300','--warmup','60','--image',image]
    execute(command,phase)
    summary=analyze(root/phase);(root/(phase+'-summary.json')).write_text(json.dumps(summary,indent=2)+'\n')
    archive(root/phase,root/'archives'/phase)
    return summary

def comparison(phase,cells='4:50,16:50,8:1000,16:1000',controls=False,clients=4,seconds=45,warmup=5,old_harness=old):
    command=[sys.executable,str(harness/'e2e/native/performance/compare.py'),
        '--slice','SP06-'+phase,'--hypothesis','Unchanged accepted SP04 runtime; measurement continuity or separately declared workload; no runtime speedup claim',
        '--predecessor-release',str(release),'--candidate-release',str(release),
        '--predecessor-revision',revision,'--candidate-revision',revision,
        '--predecessor-harness',str(harness if controls else old_harness),'--candidate-harness',str(harness),
        '--output',str(root/phase),'--cells',cells,'--clients',str(clients),'--seconds',str(seconds),
        '--warmup-seconds',str(warmup),'--repeats','3','--image',image]
    if controls:command.append('--activation-control')
    execute(command,phase)
    subprocess.run([sys.executable,str(analysis/'archive_comparison.py'),str(root/phase),str(root/'archives'/phase)],check=True)

state=json.loads((root/'campaign-status-before-controls-02.json').read_text())
assert state['phase']=='qualified-controls' and state['status']=='stopped_for_investigation'
assert 'qualified-main' in state['completed'] and 'qualified-controls' not in state['completed']
import shutil
assert shutil.disk_usage(root).free >= 64*1024**3, 'insufficient disk headroom for restart'
state['previous_failure']={k:state.pop(k) for k in ('error','error_type','exit_code') if k in state}
state['restart_review_sha256']=hashlib.sha256((root/'controls-interruption-review.json').read_bytes()).hexdigest()
state['preserved_failed_campaign']='qualified-controls'
try:
    comparison('qualified-controls-02',cells='8:1250,16:1250',controls=True,clients=8,seconds=300,warmup=60)
    state.update(status='measurements_complete_review_required',phase='final-review');save()
except BaseException as error:
    state.update(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));save()
    traceback.print_exc();raise
