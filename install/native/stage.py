#!/usr/bin/env python3
"""Sign release checksums and render install.sh; local preview keys are disposable."""
import argparse
import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent


def stage(directory, version, base_url, key):
    if key.resolve().is_relative_to(directory.resolve()):
        raise ValueError('the signing key must be outside the served directory')
    parsed = urlparse(base_url)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost'}):
        raise ValueError('use HTTPS or an explicit localhost URL')
    if not re.fullmatch(r'[A-Za-z0-9:/._-]+', base_url) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.]+)?', version):
        raise ValueError('invalid release URL/version')
    archives = list(directory.glob(f'supabricks-{version}-*.tar.gz'))
    if not archives:
        raise ValueError('no release archives to sign')
    for archive in archives:
        with archive.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        checksum_path = archive.with_name(archive.name + '.sha256')
        if checksum_path.read_text() != f'{checksum}  {archive.name}\n':
            raise ValueError('archive checksum changed since assembly')
        subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key), '-out', str(checksum_path) + '.sig', str(checksum_path)], check=True)
    public = subprocess.check_output(['openssl', 'pkey', '-in', str(key), '-pubout'], text=True).strip()
    if not public.startswith('-----BEGIN PUBLIC KEY-----'):
        raise ValueError('invalid signing key')
    script = (HERE / 'install.sh.in').read_text().replace('@VERSION@', version).replace('@BASE_URL@', base_url.rstrip('/')).replace('@PUBLIC_KEY@', public)
    (directory / 'install.sh').write_text(script)
    subprocess.run(['bash', '-n', str(directory / 'install.sh')], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--version', default='v0.1.0-alpha.4')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--signing-key', required=True, type=Path)
    args = parser.parse_args()
    stage(args.directory.resolve(), args.version, args.base_url, args.signing_key.resolve())
