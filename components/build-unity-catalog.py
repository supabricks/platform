#!/usr/bin/env python3
"""Build the reviewed UC server and private JRE from locked offline Maven inputs."""
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
import concurrent.futures
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / 'components/unity-catalog-source.lock.json'
MAVEN = ROOT / 'components/unity-catalog-maven.lock.json'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def fetch(spec, path):
    if not path.exists():
        temporary = path.with_name(path.name+'.download')
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
    # The archive checksum alone does not authenticate an extracted cache that
    # may have been edited or left incomplete by an interrupted earlier build.
    with tarfile.open(archive) as tar:
        expected=set()
        for member in tar:
            path=unpacked/member.name
            if member.isdir():
                if path.is_symlink() or not path.is_dir(): raise ValueError('Java cache directory mismatch')
                continue
            expected.add(member.name.rstrip('/'))
            if member.issym():
                if not path.is_symlink() or os.readlink(path)!=member.linkname: raise ValueError('Java cache symlink mismatch')
            elif member.isfile() or member.islnk():
                with tar.extractfile(member) as data:
                    checksum=hashlib.file_digest(data,'sha256').hexdigest()
                if path.is_symlink() or not path.is_file() or digest(path)!=checksum:
                    raise ValueError('Java cache checksum mismatch')
            else: raise ValueError('unsupported Java archive member')
        actual={str(p.relative_to(unpacked)) for p in unpacked.rglob('*') if p.is_file() or p.is_symlink()}
        if actual!=expected: raise ValueError('Java cache inventory mismatch')
    homes = [p.parent.parent for p in unpacked.rglob('bin/java')]
    if len(homes) != 1:
        raise ValueError('ambiguous Java runtime')
    return homes[0]


def stage_configuration(source, output):
    # A clean checkout may still contain ignored admin tokens/signing keys
    # from an earlier developer run. Only package tracked templates.
    tracked = subprocess.check_output(['git', 'ls-files', '-z', '--', 'etc/conf'], cwd=source).decode().split('\0')
    inventory = {}
    output.mkdir()
    for name in filter(None, tracked):
        path = source/name
        relative = Path(name).relative_to('etc/conf')
        if path.is_symlink() or not path.is_file():
            raise ValueError('configuration templates must be regular tracked files')
        destination = output/relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        inventory[str(relative)] = digest(path)
    if 'server.properties' not in inventory or 'hibernate.properties' not in inventory:
        raise ValueError('required UC configuration templates missing')
    return inventory


def maven_mirror(tools, pin):
    lock = json.loads(MAVEN.read_text())
    if lock['source_commit'] != pin['commit'] or lock['repository'] != 'https://repo.maven.apache.org/maven2/':
        raise ValueError('Maven lock does not match UC source')
    mirror = tools/'maven'
    def download(item):
        name, entry = item
        if not name or any(p in ('', '.', '..') for p in name.split('/')) or name.startswith('/'):
            raise ValueError('invalid Maven lock path')
        path = mirror/name
        path.parent.mkdir(parents=True, exist_ok=True)
        fetch(dict(url=lock['repository']+urllib.parse.quote(name, safe='/'), sha256=entry['sha256']), path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(download, lock['files'].items()))
    actual = {str(p.relative_to(mirror)) for p in mirror.rglob('*') if p.is_file()}
    if actual != set(lock['files']) or any(p.is_symlink() for p in mirror.rglob('*')):
        raise ValueError('Maven mirror contains unreviewed files or symlinks')
    return mirror


def dependencies(jars, mirror, output):
    """Preserve runtime coordinates, POM ancestry, declared licenses and notices."""
    preserved = set()
    def pom_licenses(path, seen=()):
        if path in seen or len(seen)>20: raise ValueError('cyclic POM ancestry')
        relative = path.relative_to(mirror)
        destination = output/'licenses/maven'/relative
        if relative not in preserved:
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination); preserved.add(relative)
        xml = ET.parse(path).getroot()
        for element in xml.iter(): element.tag=element.tag.rsplit('}',1)[-1]
        licenses = [dict(name=e.findtext('name',default=''),url=e.findtext('url',default='')) for e in xml.findall('licenses/license')]
        parent = xml.find('parent')
        if parent is not None:
            group=parent.findtext('groupId');artifact=parent.findtext('artifactId');version=parent.findtext('version')
            parent_path=mirror/str(group).replace('.','/')/str(artifact)/str(version)/f'{artifact}-{version}.pom'
            if parent_path.is_file():
                inherited=pom_licenses(parent_path,(*seen,path))
                if not licenses: licenses=inherited
        return licenses
    records=[]
    for jar in jars:
        try: relative=jar.relative_to(mirror)
        except ValueError: continue  # our source-built server JAR
        group='.'.join(relative.parts[:-3]);artifact,version=relative.parts[-3:-1]
        pom=jar.parent/f'{artifact}-{version}.pom'
        licenses=pom_licenses(pom)
        notices=[]
        with zipfile.ZipFile(jar) as archive:
            for name in archive.namelist():
                if name.endswith('/') or not Path(name).name.lower().startswith(('license','notice','copying','copyright')): continue
                if any(p in ('.','..') for p in Path(name).parts) or name.startswith('/'): raise ValueError('unsafe JAR notice path')
                dest=output/'licenses/jars'/jar.name/name
                dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(archive.read(name));notices.append(str(dest.relative_to(output)))
        if not licenses: raise ValueError('missing runtime license declaration: '+str(relative))
        records.append(dict(group=group,artifact=artifact,version=version,source='https://repo.maven.apache.org/maven2/'+str(relative),sha256=digest(jar),licenses=licenses,notices=notices,pom=str(pom.relative_to(mirror))))
    (output/'dependencies.json').write_text(json.dumps(records,indent=2)+'\n')


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
    mirror = maven_mirror(tools, pin)
    repositories = tools / 'repositories'
    repositories.write_text('[repositories]\nlocked: '+mirror.as_uri()+'/\n')
    # Build only git-tracked inputs, including no ignored local plugins or keys.
    checkout = tools/'source'
    if checkout.exists(): shutil.rmtree(checkout)
    checkout.mkdir()
    archive = tools/'source.tar'
    with archive.open('wb') as out:
        subprocess.run(['git','archive',sha],cwd=source,stdout=out,check=True)
    with tarfile.open(archive) as tar: tar.extractall(checkout,filter='data')
    archive.unlink()
    started = time.monotonic()
    env = {k:v for k,v in os.environ.items() if k not in ('JAVA_TOOL_OPTIONS','_JAVA_OPTIONS','JDK_JAVA_OPTIONS','CLASSPATH','SBT_OPTS') and not k.startswith('COURSIER')}
    env.update(JAVA_HOME=str(jdk),COURSIER_CACHE=str(tools/'coursier'),COURSIER_REPOSITORIES=mirror.as_uri())
    with (tools / 'build.log').open('w') as log:
        subprocess.run([str(jdk/'bin/java'), '-Xmx2g', '-XX:ActiveProcessorCount=4',
            '-Dsbt.override.build.repos=true', '-Dsbt.repository.config='+str(repositories),
            '-Dsbt.boot.directory='+str(tools/'boot'), '-Dsbt.ivy.home='+str(tools/'ivy'),
            '-Dsbt.global.base='+str(tools/'sbt'),
            '-Dsbt.supershell=false', '-DskipDeltaSpark=true', '-jar', str(launcher),
            'server/Compile/packageBin'], cwd=checkout, env=env, stdout=log,
            stderr=subprocess.STDOUT, check=True, timeout=1200)
    output.mkdir(parents=True)
    (output / 'jars').mkdir()
    # Upstream's distribution includes the CLI and embeds builder paths. Ship
    # the actual server Runtime classpath; no Spark JVM or developer cache needed.
    entries = (checkout/'server/target/classpath').read_text().split(os.pathsep)
    jars = [Path(p) for p in entries if Path(p).is_file()]
    own = [p for p in jars if p.name.startswith('unitycatalog-server-')]
    if len(own) != 1:
        raise ValueError('server jar missing/ambiguous')
    with zipfile.ZipFile(own[0]) as archive:
        for entry in entries:
            path = Path(entry)
            if path.is_dir():
                if path != checkout/'server/target/classes':
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
    shutil.copytree(jre, output/'java', symlinks=False)
    configuration = stage_configuration(source, output/'configuration-template')
    (output/'licenses').mkdir()
    for name in ('LICENSE','NOTICE'):
        shutil.copy2(source/name, output/'licenses'/name)
    dependencies(jars, mirror, output)
    report = dict(schema_version=2, scope='UC01 source-built server and private JRE',
        target=target, source_commit=sha, source_dirty=False, source_pin_sha256=digest(PIN),
        dependency_lock_sha256=digest(MAVEN), builder_script_sha256=digest(Path(__file__)),
        java=pin['java'], sbt=pin['sbt'], source_inputs=pin['inputs'],
        build_seconds=round(time.monotonic()-started, 3), jars=inventory, configuration_templates=configuration,
        dependency_lock_status='checksum-verified build/runtime Maven mirror; SBT resolves only file URLs',
        files={str(p.relative_to(output)):digest(p) for p in sorted(output.rglob('*')) if p.is_file()})
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
