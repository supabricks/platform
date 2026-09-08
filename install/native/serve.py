#!/usr/bin/env python3
"""Serve a signed installer on loopback; never put the private key in the web root."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import tempfile
from stage import stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--version', default='v0.1.0-alpha.2')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    # Binding first prevents generating a script for someone else's listener.
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(SimpleHTTPRequestHandler, directory=str(directory)))
    base = f'http://127.0.0.1:{server.server_port}'
    with tempfile.TemporaryDirectory(prefix='supabricks-preview-key-') as private:
        key = Path(private) / 'release.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', str(key)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stage(directory, args.version, base, key)
        print(f'curl -fsSL {base}/install.sh | bash', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
