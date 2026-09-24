#!/usr/bin/env python3
"""Run repeated core-scaling trials sequentially against one unchanged package.

Docker and the qualification image must already exist. Only uniquely named trial
containers and a newly created output directory are owned by this command.
"""
import argparse
import hashlib
import fcntl
import itertools
import json
import os
from pathlib import Path
import random
import subprocess
import time

ROOT=Path(__file__).resolve().parents[3]


def output(*cmd):return subprocess.check_output(cmd,text=True).strip()


def topology():
    groups={}
    allowed=os.sched_getaffinity(0)
    for line in output('lscpu','-p=CPU,CORE,SOCKET').splitlines():
        if line.startswith('#'):continue
        cpu,core,socket=map(int,line.split(','))
        if cpu in allowed:groups.setdefault((socket,core),[]).append(cpu)
    return [sorted(group) for _,group in sorted(groups.items())]


def affinity(groups,count):
    selected=[]
    for group in groups:
        selected.extend(group)
        if len(selected)==count:return sorted(selected)
        if len(selected)>count:raise ValueError('CPU count must select complete SMT sibling groups')
    raise ValueError('requested CPUs exceed available topology')


def accepted(entry):
    cleanup=entry.get('cleanup',{})
    clean=cleanup.get('remaining_descendants')==0 and cleanup.get('leaked_descendants')==0 and not cleanup.get('timed_out',True)
    # catalog_gate normalizes a nonzero child status to exit 1; the original
    # trial exit code is retained in cleanup.json. Check both reports.
    success=entry.get('status')=='measured' and entry['exit_code']==0 and cleanup.get('exit_code')==0
    failure=entry.get('status')=='runtime_failed' and entry['exit_code']==1 and cleanup.get('exit_code')==2
    return clean and (success or failure)


def main(args):
    # Refuse to mix old runs, partial results, or different binaries into a matrix.
    args.output.mkdir(parents=True,exist_ok=args.resume)
    lock=(args.output/'.controller.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    scratch=args.output/'scratch';scratch.mkdir(exist_ok=args.resume)
    release=args.release.resolve();package=json.loads((release/'release.json').read_text())
    groups=topology();sets={n:affinity(groups,n) for n in args.cpus}
    image=output('docker','image','inspect',args.image,'--format','{{.Id}}')
    matrix=list(itertools.product(range(1,args.repeats+1),args.rates,args.cpus))
    random.Random(args.seed).shuffle(matrix)
    manifest=dict(scope='local same-host CPU scaling; not EC2 emulation or release qualification',
        harness_revision=output('git','-C',str(ROOT),'rev-parse','HEAD'),
        harness_dirty=bool(output('git','-C',str(ROOT),'status','--porcelain')),
        harness_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')},
        runtime_revision=args.runtime_revision,package_provenance=package.get('provenance'),
        release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256((release/'bin/supabricks').read_bytes()).hexdigest(),
        image_id=image,host=json.loads(output('lscpu','-J')),
        cpu_siblings=groups,affinity=sets,memory_gib=args.memory_gib,swap_disabled=True,
        cpu_quota='none; affinity restriction only',network='none; loopback within each container',
        filesystem=output('findmnt','--json','-T',str(scratch),'-o','SOURCE,FSTYPE,OPTIONS'),
        host_memory_before=Path('/proc/meminfo').read_text(),seed=args.seed,
        workload=dict(profile=args.profile,rates=args.rates,seconds=args.seconds,clients=args.clients,rows_per_table=args.rows,baseline_seconds=5,warmup_seconds=5),
        order=[dict(repeat=r,rate=rate,cpus=cpus) for r,rate,cpus in matrix],started_at=time.time(),trials=[])
    # The full inventory is unchanged; keep source metadata but not thousands of
    # dependency hashes in each summary. release_identity binds that inventory.
    manifest['package_provenance']={k:v for k,v in (package.get('provenance') or {}).items() if k in ('platform_revision','platform_dirty','source_revision')}
    if args.resume:
        prior=json.loads((args.output/'matrix.json').read_text())
        if prior.get('completed_at'):raise ValueError('matrix is already complete')
        for key in ('runtime_revision','release_identity','binary_sha256','image_id','cpu_siblings','memory_gib','seed','workload','order'):
            if prior[key]!=manifest[key]:raise ValueError('resume changed '+key)
        for name in ('trial.py','test_trial.py'):
            if prior['harness_sha256'][name]!=manifest['harness_sha256'][name]:raise ValueError('resume changed trial code')
        for entry,expected in zip(prior['trials'],manifest['order']):
            if any(entry[k]!=expected[k] for k in ('repeat','rate','cpus')):raise ValueError('completed trial order differs')
            if not accepted(entry):raise ValueError('cannot resume after a measurement or cleanup failure')
        prior.setdefault('resume_history',[]).append(dict(at=time.time(),controller_sha256=manifest['harness_sha256']['matrix.py'],reason='resume with unchanged trial code, workload, image and runtime'))
        manifest=prior
    def save():
        (args.output/'matrix.json').write_text(json.dumps(manifest,indent=2)+'\n')
    save()
    prefix='sb-scale-'+str(os.getpid())
    completed=len(manifest['trials'])
    for index,(repeat,rate,cpus) in enumerate(matrix,1):
        if index<=completed:continue
        trial=f'{index:02}-cpu{cpus}-rate{rate}-r{repeat}'
        report=args.output/trial;report.mkdir()
        name=prefix+'-'+str(index)
        command=['docker','run','--rm','--init','--name',name,'--network','none',
            '--cpuset-cpus',','.join(map(str,sets[cpus])),'--memory',f'{args.memory_gib}g','--memory-swap',f'{args.memory_gib}g',
            '--user',f'{os.getuid()}:{os.getgid()}',
            '-v',f'{ROOT}:/repo:ro','-v',f'{release}:/release:ro','-v',f'{report.resolve()}:/reports',
            '-v',f'{scratch.resolve()}:/scratch','-w','/repo',image,
            'python3','install/native/catalog_gate.py','--timeout','600','--report','/reports/cleanup.json','--',
            'python3','e2e/native/performance/trial.py','--release','/release','--report','/reports/trial.json',
            '--scratch','/scratch','--rate',str(rate),'--seconds',str(args.seconds),'--clients',str(args.clients),'--rows',str(args.rows)]
        if args.profile:command.append('--profile')
        print(f'[{index}/{len(matrix)}] {trial} start',flush=True)
        start=time.time()
        with (report/'private.log').open('w') as stream:
            try:
                result=subprocess.run(command,stdout=stream,stderr=subprocess.STDOUT,timeout=660)
                code=result.returncode
            except (subprocess.TimeoutExpired,KeyboardInterrupt):
                # Never touch containers outside this matrix's unique name.
                subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                manifest['interrupted_trial']=trial;save();raise
        entry=dict(name=trial,cpus=cpus,rate=rate,repeat=repeat,exit_code=code,seconds=round(time.time()-start,2))
        if (report/'trial.json').exists():
            measured=json.loads((report/'trial.json').read_text())
            entry.update(status=measured['status'],runtime_error=measured.get('runtime_error'),lag=measured.get('stages_ms',{}).get('commit_to_publication'),
                achieved=measured.get('source',{}).get('achieved_rows_per_second'),
                cpu=measured.get('cpu',{}).get('average_cpu_cores'),
                peak_memory_bytes=measured.get('peak_memory_bytes'),within_5s_p95=measured.get('within_5s_p95'),
                offered_load_met=measured.get('offered_load_met'))
        if (report/'cleanup.json').exists():entry['cleanup']=json.loads((report/'cleanup.json').read_text())
        manifest['trials'].append(entry);save()
        print(json.dumps(entry),flush=True)
        # Keep failures as evidence. More load is not useful after correctness or
        # cleanup failure; a lag or offered-rate miss is a valid measured trial.
        if not accepted(entry):raise SystemExit('Measurement or cleanup failed; inspect private log before continuing')
    manifest['completed_at']=time.time();save()
    print('MATRIX_COMPLETE '+str(args.output/'matrix.json'),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile',action='store_true',help='enable bounded diagnostic instrumentation')
    p.add_argument('--release',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--runtime-revision',required=True)
    p.add_argument('--resume',action='store_true',help='continue a stopped matrix only if completed trials and identities validate')
    p.add_argument('--image',default='supabricks-sy08-qualifier:latest')
    p.add_argument('--cpus',type=int,nargs='+',default=[4,8,16]);p.add_argument('--memory-gib',type=int,default=16)
    p.add_argument('--rates',type=int,nargs='+',default=[50,250,1000]);p.add_argument('--seconds',type=int,default=45)
    p.add_argument('--repeats',type=int,default=3);p.add_argument('--clients',type=int,default=4);p.add_argument('--rows',type=int,default=10000)
    p.add_argument('--seed',type=int,default=20260923)
    a=p.parse_args()
    if min([a.memory_gib,a.seconds,a.repeats,a.clients,a.rows,*a.cpus,*a.rates])<1:p.error('all dimensions must be positive')
    main(a)
