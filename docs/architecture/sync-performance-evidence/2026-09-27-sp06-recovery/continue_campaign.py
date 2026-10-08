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
state={'phase':'source-screen','status':'waiting_for_existing_controller','completed':[],
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

try:
    save()
    # Wait for the real controller lock, rather than inferring completion from
    # a log line or relaunching a trial after an interruption.
    with (root/'source-screen/.controller.lock').open('a') as screen_lock:
        while True:
            try:fcntl.flock(screen_lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:time.sleep(10)
    record=json.loads((root/'source-screen/experiment.json').read_text())
    if record['state']!='complete':
        archive(root/'source-screen',root/'archives/source-screen',allow_incomplete=True)
        raise RuntimeError('source screen stopped incomplete; investigate before new measurements')
    screen=analyze(root/'source-screen')
    (root/'source-screen-summary.json').write_text(json.dumps(screen,indent=2)+'\n')
    archive(root/'source-screen',root/'archives/source-screen')
    state['completed'].append('source-screen')
    selection=screen['provisional_selection'];state['provisional_selection']=selection;save()
    selected=selection['clients'] if selection else 16
    qualified=False
    if selection:
        control=source_phase('source-controls','controls',[8,16],[selected])
        floor=selection['qualification_floor_rows_s']
        for t in control['trials']:
            if min(t['minute_rows_s'])<floor:
                state['review_flags'].append({'phase':'source-controls','reason':'capacity threshold crossing','trial':t['directory']})
        for cpu in control['controls']:
            for key,v in cpu['paired_percent'].items():
                if key in ('transaction_p95_ms','cpu_cores','peak_memory_bytes') and v['median']>10:
                    state['review_flags'].append({'phase':'source-controls','cpu':cpu['cpus'],'reason':'over 10 percent observer regression','metric':key,'median':v['median']})
                if key in ('rows_s','min_minute_rows_s') and v['maximum']<0:
                    state['review_flags'].append({'phase':'source-controls','cpu':cpu['cpus'],'reason':'repeatable throughput loss; investigate','metric':key,'median':v['median']})
        qualified=not state['review_flags']
        state['source_controls_clear']=qualified;save()
    else:
        state['review_flags'].append({'phase':'source-screen','reason':'no tested client count sustains 1000 changed rows/s in every required minute'})
    source_phase('source-envelope','envelope',[4],sorted({4,selected}))
    comparison('historical-main')
    comparison('historical-controls',cells='4:50,8:1000,16:1000',controls=True)
    if qualified:
        state['separate_profile']={'clients':selected,'offered_rows_s':1250,'warmup_seconds':60,'seconds':300,'rows_per_table':10000,'changes_per_transaction':2,'source_floor_rows_s':selection['qualification_floor_rows_s']};save()
        comparison('qualified-main',cells='8:1250,16:1250',clients=selected,seconds=300,warmup=60,old_harness=harness)
        comparison('qualified-controls',cells='8:1250,16:1250',controls=True,clients=selected,seconds=300,warmup=60)
    else:
        state['review_flags'].append({'phase':'qualified-profile','reason':'not run until source capacity/observer concerns are resolved'})
    state.update(status='measurements_complete_review_required',phase='review');save()
except BaseException as error:
    state.update(status='stopped_for_investigation',error_type=type(error).__name__,error=str(error));save()
    traceback.print_exc();raise
