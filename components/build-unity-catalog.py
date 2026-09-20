#!/usr/bin/env python3
"""UC00 source build/probe staging. Does not modify the installed platform."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / 'components/unity-catalog-source.lock.json'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch(spec, path):
    if not path.exists():
        temporary = path.with_suffix('.download')
        with urllib.request.urlopen(spec['url'], timeout=90) as src, temporary.open('wb') as dst:
            shutil.copyfileobj(src, dst)
        temporary.rename(path)
    if digest(path) != spec['sha256']:
        raise ValueError(f'checksum mismatch: {path.name}')
    return path


def java_home(cache, spec, kind):
    archive = fetch(spec, cache / (kind + '.tar.gz'))
    unpacked = cache / (kind + '-' + spec['sha256'][:12])
    if not unpacked.exists():
        unpacked.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(unpacked, filter='data')
    homes = [p.parent.parent for p in unpacked.rglob('bin/java')]
    if len(homes) != 1:
        raise ValueError('ambiguous Java runtime')
    return homes[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    target = {('Linux', 'x86_64'): 'linux-x86_64', ('Darwin', 'arm64'): 'macos-arm64'}[
        (platform.system(), platform.machine())]
    source, tools, output = (p.resolve() for p in (args.source, args.tools, args.output))
    pin = json.loads(PIN.read_text())
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    if sha != pin['commit'] or subprocess.check_output(['git', 'status', '--porcelain'], cwd=source):
        raise ValueError('UC source is dirty or differs from reviewed pin')
    for name, expected in pin['inputs'].items():
        if digest(source / name) != expected:
            raise ValueError(f'UC source input mismatch: {name}')
    if output.exists():
        raise ValueError('output must be new')
    tools.mkdir(parents=True, exist_ok=True)
    java = pin['java']['targets'][target]
    jdk = java_home(tools, java['jdk'], 'jdk')
    jre = java_home(tools, java['jre'], 'jre')
    launcher = fetch(pin['sbt'], tools / 'sbt-launch.jar')
    repositories = tools / 'repositories'
    repositories.write_text('[repositories]\ncentral: https://repo.maven.apache.org/maven2/\n')
    started = time.monotonic()
    env = dict(os.environ, JAVA_HOME=str(jdk))
    with (tools / 'build.log').open('w') as log:
        subprocess.run([str(jdk/'bin/java'), '-Xmx2g', '-XX:ActiveProcessorCount=4',
            '-Dsbt.override.build.repos=true', '-Dsbt.repository.config='+str(repositories),
            '-Dsbt.supershell=false', '-DskipDeltaSpark=true', '-jar', str(launcher),
            'server/Compile/packageBin'], cwd=source, env=env, stdout=log,
            stderr=subprocess.STDOUT, check=True, timeout=1200)
    output.mkdir(parents=True)
    (output / 'jars').mkdir()
    # Upstream's distribution includes the CLI and embeds builder paths. Ship
    # the actual server Runtime classpath; no Spark JVM or developer cache needed.
    entries = (source/'server/target/classpath').read_text().split(os.pathsep)
    jars = [Path(p) for p in entries if Path(p).is_file()]
    own = [p for p in jars if p.name.startswith('unitycatalog-server-')]
    if len(own) != 1:
        raise ValueError('server jar missing/ambiguous')
    with zipfile.ZipFile(own[0]) as archive:
        for entry in entries:
            path = Path(entry)
            if path.is_dir():
                if path != source/'server/target/classes':
                    raise ValueError('unpackaged runtime class directory')
                for file in path.rglob('*'):
                    if file.is_file() and archive.read(str(file.relative_to(path))) != file.read_bytes():
                        raise ValueError('server jar does not contain runtime class inventory')
    inventory = []
    for i, jar in enumerate(jars):
        relative = f'jars/{i:03d}-{jar.name}'
        shutil.copy2(jar, output / relative)
        inventory.append(dict(path=relative, source_name=jar.name, sha256=digest(jar), bytes=jar.stat().st_size))
    (output/'classpath.json').write_text(json.dumps([x['path'] for x in inventory])+'\n')
    shutil.copytree(jre, output/'java', symlinks=True)
    shutil.copytree(source/'etc/conf', output/'configuration-template')
    (output/'licenses').mkdir()
    for name in ('LICENSE','NOTICE'):
        shutil.copy2(source/name, output/'licenses'/name)
    # JRE legal/ is preserved; third-party JARs retain their embedded notices.
    report = dict(schema_version=1, scope='UC00 server-only developer artifact',
        target=target, source_commit=sha, source_pin_sha256=digest(PIN),
        java=pin['java'], sbt=pin['sbt'], source_inputs=pin['inputs'],
        build_seconds=round(time.monotonic()-started, 3), jars=inventory,
        dependency_lock_status='resolved JAR hashes recorded; production transitive lock is UC01 work')
    (output/'build.json').write_text(json.dumps(report, indent=2)+'\n')
    artifact = output.with_suffix('.tar.gz')
    with tarfile.open(artifact, 'w:gz') as tar:
        tar.add(output, arcname='uc00-runtime')
    artifact_report = dict(target=target, source_commit=sha,
        compressed_bytes=artifact.stat().st_size, artifact_sha256=digest(artifact))
    output.with_suffix('.artifact.json').write_text(json.dumps(artifact_report, indent=2)+'\n')
    print(json.dumps(artifact_report))



if __name__ == '__main__':
    main()
