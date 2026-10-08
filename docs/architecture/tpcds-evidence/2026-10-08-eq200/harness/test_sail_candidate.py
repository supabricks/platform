import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sail_candidate import digest, validate
from verify import release_provenance
from inputs import sha


class SailCandidateChecks(unittest.TestCase):
    def test_rejects_unrelated_changes_tampering_and_unproven_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);old=root/'old';new=root/'new';load=root/'load'
            old.mkdir();new.mkdir();(load/'state').mkdir(parents=True)
            native='python/runtime/lib/python3.12/site-packages/pysail/_native.so'
            expected={native:b'candidate'}
            report=dict(version='0.7.1',commit='reviewed',wheel={'sha256':'wheel'},source_lock_sha256='lock')
            before=dict(version='v1',target='linux-x86_64',provenance={'sail':dict(report,commit='old')},
                        files={native:dict(sha256=digest(b'old'),executable=True),
                               'bin/supabricks':dict(sha256='native',executable=True),
                               'delta.py':dict(sha256='delta',executable=False)})
            after=copy.deepcopy(before);after['version']='v2';after['provenance']['sail']=report
            after['files'][native]['sha256']=digest(expected[native])
            (new/native).parent.mkdir(parents=True);(new/native).write_bytes(expected[native])
            with patch('sail_candidate.replacement_files',return_value=(expected,report)):
                self.assertEqual(validate(before,after,new,root)['changed_payload_files'],[native])
                for change in ('delta','format','executable','provenance','inventory','wheel'):
                    bad=copy.deepcopy(after)
                    if change=='delta':bad['files']['delta.py']['sha256']='changed'
                    elif change=='format':bad['formats']={'delta':3}
                    elif change=='executable':bad['files'][native]['executable']=False
                    elif change=='provenance':bad['provenance']['sail']['commit']='unreviewed'
                    elif change=='inventory':del bad['files']['delta.py']
                    elif change=='wheel':bad['files'][native]['sha256']='other'
                    with self.subTest(change=change),self.assertRaises(AssertionError):validate(before,bad,new,root)
                (new/native).write_bytes(b'tampered')
                with self.assertRaises(AssertionError):validate(before,after,new,root)
                (new/native).write_bytes(expected[native])
                def save(path,value):path.write_text(json.dumps(value))
                save(old/'release.json',before);save(new/'release.json',after)
                loaded={'release_identity':sha(old/'release.json')}
                receipt={'from':{'identity':loaded['release_identity']},'to':{'identity':sha(new/'release.json')},
                         'backup':'/backup','backup_id':'retained-stopped-copy'}
                save(load/'state/last-upgrade.json',receipt)
                save(load/'state/runtime.json',{'installation_identity':sha(new/'release.json')})
                self.assertEqual(release_provenance(loaded,new,load,old,root)['sail_commit'],'reviewed')
                with self.assertRaises(AssertionError):release_provenance(loaded,new,load,old)
                with self.assertRaises(AssertionError):release_provenance(loaded,new,load,sail_artifact=root)
                receipt['from']['identity']='wrong';save(load/'state/last-upgrade.json',receipt)
                with self.assertRaises(AssertionError):release_provenance(loaded,new,load,old,root)


if __name__=='__main__':unittest.main()
