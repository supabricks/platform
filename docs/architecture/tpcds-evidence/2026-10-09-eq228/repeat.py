import os,subprocess
from pathlib import Path
release='build/eq220/programs/releases/v0.1.0-alpha.36.eq226serial'
for pair in range(1,4):
 for candidate in ('baseline','candidate') if pair%2 else ('candidate','baseline'):
  for mode in ('cold','warm'):
   env=dict(os.environ)
   if candidate=='candidate':env['EQ228_SOURCE']=str(Path('python/analytics').resolve())
   subprocess.run(['taskset','-c','0-7',release+'/python/analytics/python','build/eq228/probe.py',release,'<evidence-root>/eq228/fixture02',f'<evidence-root>/eq228/{candidate}-{mode}-{pair}',mode],env=env,check=True)
