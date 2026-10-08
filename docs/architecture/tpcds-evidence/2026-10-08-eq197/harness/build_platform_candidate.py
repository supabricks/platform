#!/usr/bin/env python3
"""Build the narrow native/session-worker artifact from a clean committed tree."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess

from inputs import sha
from platform_candidate import ROOT, WORKER, replacement_files


def build(output):
    assert not output.exists()
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()
    assert not git('status', '--porcelain'), 'commit platform source before building'
    commit = git('rev-parse', 'HEAD')
    command = ['cargo', 'build', '--release', '--locked', '-p', 'supabricks-local']
    output.mkdir(parents=True)
    with (output / 'build.log').open('w') as log:
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    assert git('rev-parse', 'HEAD') == commit and not git('status', '--porcelain'), 'source changed during build'
    for name, source in [('bin/supabricks', ROOT / 'target/release/supabricks'), (WORKER, ROOT / WORKER)]:
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
    report = dict(commit=commit, source_dirty=False, command=command,
                  rustc=subprocess.check_output(['rustc', '--version'], text=True).strip(),
                  cargo=subprocess.check_output(['cargo', '--version'], text=True).strip(),
                  cargo_lock_sha256=sha(ROOT / 'Cargo.lock'),
                  build_log_sha256=sha(output / 'build.log'),
                  files={name: sha(output / name) for name in ['bin/supabricks', WORKER]})
    (output / 'platform-build.json').write_text(json.dumps(report, indent=2) + '\n')
    replacement_files(output)
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output.resolve())
