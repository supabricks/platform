from pathlib import Path
import csv,statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
root=Path('docs/architecture/sync-performance-evidence')
baseline=list(csv.DictReader((root/'2026-09-24-local/trials.csv').open()))
new=root/'2026-09-24-quiet-followup'
fresh={r:list(csv.DictReader((new/f'rate{r}'/'trials.csv').open())) for r in (50,1000)}
plt.rcParams.update({'font.size':10,'axes.spines.top':False,'axes.spines.right':False,'svg.hashsalt':'supabricks-quiet-followup'})
fig,axes=plt.subplots(1,2,figsize=(10,4.8),layout='constrained')
colors=['#7c3aed','#0891b2']
for ax,rate,cpus,key,scale in [(axes[0],50,[4,16],'lag_p95_ms',1000),(axes[1],1000,[8,16],'achieved',1)]:
 for j,cpu in enumerate(cpus):
  for k,(label,rows) in enumerate([('Original (all repeats)',baseline),('After build finished',fresh[rate])]):
   selected=[r for r in rows if int(r['rate'])==rate and int(r['cpus'])==cpu]
   x=j+(-.14 if k==0 else .14)
   values=[float(r[key])/scale for r in selected if r[key]]
   for i,r in enumerate(selected):
    if r[key]:
     flagged=r.get('host_contention_flagged')=='True'
     ax.scatter(x+(i-1)*.035,float(r[key])/scale,marker='x' if flagged else 'o',color=colors[k],s=45,zorder=3)
   if values:ax.plot([x-.065,x+.065],[statistics.median(values)]*2,color=colors[k],lw=2)
   if j==0:ax.scatter([],[],color=colors[k],label=label)
   if rate==1000:
    missing=sum(not r[key] for r in selected)
    if missing:ax.text(x,35,f'{missing} warmup\nfailure(s)',ha='center',fontsize=8,color=colors[k])
 ax.set_xlim(-.5,len(cpus)-.5);ax.set_xticks(range(len(cpus)),[str(c) for c in cpus]);ax.set_xlabel('Available logical CPUs (paired SMT)');ax.grid(axis='y',alpha=.15)
from matplotlib.ticker import NullFormatter
axes[0].yaxis.set_minor_formatter(NullFormatter())
axes[0].set_ylabel('Trial p95 publication lag (seconds; log scale)');axes[0].set_yscale('log');axes[0].set_yticks([3,5,10,50],labels=['3','5','10','50']);axes[0].yaxis.set_minor_formatter(NullFormatter());axes[0].axhline(5,color='#b91c1c',ls='--',lw=1);axes[0].set_title('50 changed rows/s · all complete')
axes[1].set_ylabel('Achieved source writes (changed rows/s)');axes[1].axhline(1000,color='#b91c1c',ls='--',lw=1);axes[1].set_ylim(0,1100);axes[1].set_title('1,000 offered rows/s · failures retained')
axes[0].legend(fontsize=8)
fig.suptitle('Matched follow-up · same runtime and workload · three fresh repeats',fontsize=13)
fig.supxlabel('Dots are individual trials; short lines are medians of available values. × marks identified original build overlap.\nSource input is not replication throughput. Desktop host; no dedicated CPU or disk reservation.',fontsize=8)
fig.savefig(new/'comparison.png',dpi=180);fig.savefig(new/'comparison.svg',metadata={'Date':None})
p=new/'comparison.svg';p.write_text('\n'.join(x.rstrip() for x in p.read_text().splitlines())+'\n')
