"""Run installed sync lifecycle suites with the explicit large storage profile.

Only policy creation changes; the original assertions, installed workers and
resource guards are retained. Both API and CLI creation assert the resulting
profile so these checks cannot silently exercise only synchronous compact apply.
"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
import installed_sync


class LargeProfile:
    def checked_profile(self, policy):
        assert policy['config']['storage_profile']=='large'
        self.metrics['storage_profile']='large'
        self.metrics['large_policies_created']=self.metrics.get('large_policies_created',0)+1
        return policy

    def cli(self,*args):
        create=args[:2]==('sync','create')
        if create:args=(*args,'--storage-profile','large')
        result=super().cli(*args)
        return self.checked_profile(result) if create else result

    def sync(self,kind,**fields):
        if kind=='create':fields['config']=dict(fields['config'],storage_profile='large')
        result=super().sync(kind,**fields)
        return self.checked_profile(result) if kind=='create' else result


if __name__=='__main__':
    installed_sync.SUITES={name:type('Large'+suite.__name__,(LargeProfile,suite),{})
                          for name,suite in installed_sync.SUITES.items()
                          if name in ('continuous','triggered','maintenance')}
    installed_sync.main()
