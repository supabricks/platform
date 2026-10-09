import datetime,hashlib,io,json,os,shlex,subprocess,sys,tarfile,time
from pathlib import Path
repo=Path.cwd();label=sys.argv[1];release=Path(sys.argv[2]).resolve()
base=Path('<evidence-root>/eq220');control=base/(label+'-control');control.mkdir(mode=0o700)
output=base/label;harness=control/'harness';harness.mkdir()
assert subprocess.check_output(['docker','inspect','eq03-sf100-load-01','--format','{{.State.Paused}}'],text=True).strip()=='true'
revision=sys.argv[3] if len(sys.argv)>3 else subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
archive=subprocess.check_output(['git','archive',revision,'e2e/native','e2e/tpcds','install/native'])
(control/'harness.tar').write_bytes(archive)
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:tar.extractall(harness,filter='data')
image='sha256:6ab9f17da0cb0203e98eac65f70f17ca8cc8d8c2aff25248a1082488dbbb23ec'
command=['python3',str(harness/'install/native/catalog_gate.py'),'--timeout','8100','--data-root',str(output/'state'),'--report',str(control/'cleanup.json'),'--','python3',str(harness/'e2e/tpcds/load.py'),'--workload','sf100-growing-prefix','--release',str(release),'--inputs',str(repo/'build/eq00-20261006/inputs'),'--dataset','<evidence-root>/sf100/gen-01','--output',str(output)]
docker=['docker','run','--detach','--name','eq220-'+label,'--network','none','--user','1000:1000','--cpuset-cpus','0-7','--memory','16g','--memory-swap','16g','--log-opt','max-size=10m','-v',f'{repo}:{repo}:ro','-v',f'{base}:{base}','-v','<evidence-root>/sf100/gen-01:<evidence-root>/sf100/gen-01:ro','-w',str(repo),image,'sh','-c','umask 077; exec '+shlex.join(command)+' > '+shlex.quote(str(control/'run.log'))+' 2>&1']
receipt=dict(status='STARTING',started_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),harness_revision=revision,harness_archive_sha256=hashlib.sha256(archive).hexdigest(),runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),release_identity=hashlib.sha256((release/'release.json').read_bytes()).hexdigest(),command=docker)
def save():
 temp=control/'launch.tmp';temp.write_text(json.dumps(receipt,indent=2)+'\n');temp.replace(control/'launch.json')
save();container=subprocess.check_output(docker,text=True).strip();receipt.update(status='RUNNING',container=container);save()
info=json.loads(subprocess.check_output(['docker','inspect',container],text=True))[0]
pid=info['State']['Pid'];group=Path('/sys/fs/cgroup')/Path('/proc',str(pid),'cgroup').read_text().strip().split('::',1)[1].lstrip('/')
wait=subprocess.Popen(['docker','wait',container],stdout=subprocess.PIPE,text=True)
with (control/'resources.jsonl').open('x') as log:
 while wait.poll() is None:
  sample=dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
  for name in ['cpu.stat','memory.current','memory.peak','memory.stat','io.stat']:
   try:sample[name]= (group/name).read_text()
   except OSError:pass
  # Fixed-label process counters only: never export argv, SQL or credentials.
  processes=[]
  try:
   for raw_pid in (group/'cgroup.procs').read_text().split()[:2048]:
    try:
     proc=Path('/proc')/raw_pid;fields=(proc/'stat').read_text().rsplit(')',1)[1].split()
     role=(proc/'comm').read_text().strip();argv=(proc/'cmdline').read_bytes()
     for worker in ['capture_worker.py','incremental_worker.py','prepare_worker.py','export.py']:
      if ('/'+worker).encode() in argv:role=worker;break
     processes.append(dict(pid=int(raw_pid),start_ticks=int(fields[19]),role=role,
                           user_ticks=int(fields[11]),system_ticks=int(fields[12]),
                           rss_bytes=max(0,int(fields[21]))*os.sysconf('SC_PAGE_SIZE')))
    except (OSError,ValueError,IndexError):pass
  except OSError:pass
  sample['processes']=processes;sample['clock_ticks_per_second']=os.sysconf('SC_CLK_TCK')
  try:
   r=json.loads((output/'result.json').read_text());sample['load']={k:r.get(k) for k in ['stage','status','elapsed_seconds','committed_rows']};sample['published_rows']=(r.get('latest',{}).get('publication') or {}).get('rows')
  except (OSError,ValueError):pass
  # Observation only, never a quiet-period admission gate.
  competing=[]
  for comm in Path('/proc').glob('[0-9]*/comm'):
   try:
    name=comm.read_text().strip()
    if name in ['cargo','rustc']:competing.append(dict(pid=int(comm.parent.name),name=name))
   except OSError:pass
  sample['observed_compilers']=competing
  log.write(json.dumps(sample)+'\n');log.flush();time.sleep(2)
exit_code=int(wait.communicate()[0].strip())
receipt.update(status='FINISHED' if exit_code==0 else 'FAILED',exit_code=exit_code,finished_utc=datetime.datetime.now(datetime.timezone.utc).isoformat());save()
info=json.loads(subprocess.check_output(['docker','inspect',container],text=True))[0]
(control/'container-final.json').write_text(json.dumps(dict(state=info['State'],limits={k:info['HostConfig'][k] for k in ['CpusetCpus','Memory','MemorySwap','NetworkMode']}),indent=2)+'\n')
print(json.dumps(receipt),flush=True)
raise SystemExit(exit_code)
