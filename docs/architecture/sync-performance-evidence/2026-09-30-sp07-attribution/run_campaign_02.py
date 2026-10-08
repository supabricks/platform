"""SP07 frozen observer control; retain failures and stop for investigation."""
from pathlib import Path
import fcntl,hashlib,json,subprocess,sys,traceback,time
root=Path(__file__).resolve().parent
repo=root.parent.parent
harness=root/'harness-02'
workload=repo/'build/sp06-recovery-20260927/harness'
release=repo/'build/sp06-recovery-20260927/accepted-runtime'
revision='e10d5152f5681af7c233701e0fd75c100cbf4c6c'
image='sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec'
lock=(root/'.campaign.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
state=dict(status='prepared',completed=[],runtime_revision=revision,image=image,
    controller_revision=subprocess.check_output(['git','-C',str(harness),'rev-parse','HEAD'],text=True).strip(),
    workload_revision='be4701cce5694ed00349ab3db9b577592965d7d7',
    driver_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
def save():
    p=root/'status.json';t=p.with_suffix('.tmp');t.write_text(json.dumps(state,indent=2)+'\n');t.replace(p)
def phase(name,seconds,warmup,rate,repeats,quiet):
    state.update(phase=name,status='running');save()
    command=[sys.executable,str(harness/'e2e/native/performance/compare.py'),
        '--slice','SP07-'+name,'--hypothesis','Host I/O observer off/on with identical runtime and existing worker profiling; attribution only, not runtime speedup or replacement for SP06 misses',
        '--predecessor-release',str(release),'--candidate-release',str(release),
        '--predecessor-revision',revision,'--candidate-revision',revision,
        '--predecessor-harness',str(workload),'--candidate-harness',str(workload),
        '--output',str(root/name),'--cells','8:'+str(rate),'--clients','8','--seconds',str(seconds),
        '--warmup-seconds',str(warmup),'--repeats',str(repeats),'--image',image,
        '--host-io','candidate','--minimum-free-gib','64','--quiet-seconds',str(quiet)]
    print('PHASE_START',name,flush=True)
    with (root/(name+'.log')).open('x') as log:
        result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,cwd=repo)
    archive=[sys.executable,str(harness/'e2e/native/performance/archive_comparison.py'),str(root/name),str(root/'archives'/name)]
    if result.returncode:
        state.update(status='stopped_for_investigation',exit_code=result.returncode);save()
        if (root/name/'experiment.json').exists():subprocess.run(archive+['--allow-incomplete'],check=True)
        raise RuntimeError('phase failed: '+name)
    subprocess.run(archive,check=True)
    state['completed'].append(name);save();print('PHASE_COMPLETE',name,flush=True)
try:
    phase('functional-smoke-02',10,5,50,1,1)
    smoke=json.loads((root/'functional-smoke-02/experiment.json').read_text())
    assert smoke['state']=='complete' and len(smoke['pairs'])==1
    # The short functional check never enters a capacity/performance denominator.
    state['smoke_scope']='functional only; excluded from performance decisions';save()
    phase('observer-controls-02',300,60,1250,3,300)
    state.update(status='measurements_complete_review_required',phase='review');save()
except BaseException as error:
    state.update(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));save()
    traceback.print_exc();raise
