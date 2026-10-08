import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
import psutil
root=Path.cwd();out=root/'build/eq03-publication/measurements-02';out.mkdir(exist_ok=False)
fixture=root/'target/debug/deps/analytics-cdaaf7692fe26d0c'
packages={'predecessor':root/'build/eq03-issue184/bounded-merge-runtime-03','candidate':root/'build/eq03-publication/runtime-01'}
# Bare copies isolate the daemon protocol fixture from installed-package startup.
# Payload hashes remain identical to the verified packages.
binaries={}
for name,package in packages.items():
 target=out/(name+'-supabricks');shutil.copy2(package/'bin/supabricks',target);binaries[name]=target
reports=[]
for rep in range(1,4):
 for name in (['predecessor','candidate'] if rep%2 else ['candidate','predecessor']):
  binary=binaries[name];label=f'{rep:02}-{name}';log=out/(label+'.log')
  env=dict(os.environ,SB_PUBLICATION_BENCH_BINARY=str(binary));seen={};start=time.monotonic()
  with log.open('w') as stream:
   child=subprocess.Popen([str(fixture),'daemon_streams_large_publication_and_rejects_tail_corruption','--exact','--nocapture'],env=env,stdout=stream,stderr=subprocess.STDOUT)
   parent=psutil.Process(child.pid)
   while child.poll() is None:
    for p in parent.children(recursive=True):
     try:
      if p.name()!='supabricks':continue
      key=(p.pid,p.create_time());cpu=p.cpu_times();r=seen.setdefault(key,dict(cpu_seconds=0,rss_peak=0,samples=0))
      r['cpu_seconds']=max(r['cpu_seconds'],cpu.user+cpu.system);r['rss_peak']=max(r['rss_peak'],p.memory_info().rss);r['samples']+=1
     except psutil.NoSuchProcess:pass
    if time.monotonic()-start>120:child.kill();raise RuntimeError('fixture timeout')
    time.sleep(.02)
  assert child.returncode==0,log.read_text()
  values=[json.loads(line.split('publication_measurement ',1)[1]) for line in log.read_text().splitlines() if line.startswith('publication_measurement ')]
  assert len(values)==2
  r=dict(repetition=rep,variant=name,binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),release_identity=hashlib.sha256((packages[name]/'release.json').read_bytes()).hexdigest(),measurements=values,daemon_samples=list(seen.values()),sampling='20ms process CPU/RSS samples; final short-lived peaks may be missed',fixture='128MiB synthetic file inventory, real daemon, one successful publication plus one tail-corruption rejection; not Delta or full sync throughput')
  (out/(label+'.json')).write_text(json.dumps(r,indent=2)+'\n');reports.append(r)
  print(label,values,flush=True)
(out/'summary.json').write_text(json.dumps(reports,indent=2)+'\n')
