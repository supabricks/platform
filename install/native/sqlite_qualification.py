"""Gate the loaded production SQLite identities against the reviewed native pins."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / 'components/sqlite-policy.json'
PROBE = Path(__file__).with_name('sqlite_probe.py')
CHECKS = {'full_delete_roundtrip', 'rollback_preserves_committed', 'integrity_check', 'read_only_refuses_write'}


def require(value, message):
    if not value:
        raise ValueError('SQLite qualification: ' + message)


def wal_reset_fixed(version):
    if not isinstance(version, str) or not re.fullmatch(r'3\.\d+\.\d+', version):
        return False
    _, minor, patch = map(int, version.split('.'))
    # Reviewed advisory branches; 3.52.0 was withdrawn, so do not accept it.
    return minor >= 53 or (minor == 51 and patch >= 3) or (minor == 50 and patch >= 7) or (minor == 44 and patch >= 6)


def validate(identity, role, policy=None):
    require(isinstance(identity, dict), 'missing ' + role + ' identity')
    policy = json.loads(POLICY.read_text()) if policy is None else policy
    require(policy.get('format_version') == 1, 'unknown policy format')
    expected = policy['runtimes'][role]
    require(identity.get('version') == expected['version'], role + ' version differs from reviewed pin')
    require(identity.get('source_id') == expected['source_id'], role + ' source ID differs from reviewed pin')
    require(wal_reset_fixed(identity['version']), role + ' lacks the reviewed WAL-reset fix')
    options = identity.get('compile_options')
    require(isinstance(options, list) and options and all(isinstance(v, str) for v in options), 'missing compile options')
    require('THREADSAFE=1' in options and 'OMIT_WAL' not in options, role + ' unsupported SQLite build')
    if role == 'python':
        require(identity.get('journal_mode') == 'delete' and identity.get('synchronous') == 2, 'changed capture durability qualification')
        require(CHECKS <= set(identity.get('checks', [])), 'missing Python roundtrip checks')
    return identity


def python_identity(executable):
    return json.loads(subprocess.check_output([str(executable), '-I', '-B', str(PROBE)], text=True))


def collect(release, verification):
    policy = json.loads(POLICY.read_text())
    require(verification.get('verified') is True, 'installation is not verified')
    require((release / 'provenance/sqlite-policy.json').read_bytes() == POLICY.read_bytes(), 'installed SQLite policy differs')
    rust = validate(verification.get('sqlite', {}), 'rust', policy)
    python = validate(python_identity(release / 'python/analytics/python'), 'python', policy)
    with (release / 'python/runtime/bin/python3.12').open('rb') as stream:
        expected_python = hashlib.file_digest(stream, 'sha256').hexdigest()
    require(python['python_executable_sha256'] == expected_python, 'worker ran an unexpected interpreter')
    reader = python_identity(sys.executable)
    return dict(format_version=1, status='passed', release_identity=verification['identity'],
                policy_sha256=hashlib.sha256(POLICY.read_bytes()).hexdigest(),
                rust=rust, python=python,
                qualification_reader=dict(identity=reader, wal_reset_fixed=wal_reset_fixed(reader['version']),
                    scope='Harness interpreter, not the production worker. Live spool reads use mode=ro; stopped corruption fixtures use DELETE. No permission to write/checkpoint a live WAL database is established.'),
                probe_journal_mode='delete', probe_synchronous=2,
                wal_mode_qualified=False)


def validate_evidence(record, release_identity):
    require(isinstance(record, dict) and record.get('format_version') == 1 and record.get('status') == 'passed', 'missing installed evidence')
    require(record.get('release_identity') == release_identity, 'mixed installed SQLite evidence')
    require(record.get('policy_sha256') == hashlib.sha256(POLICY.read_bytes()).hexdigest(), 'unreviewed SQLite policy')
    require(record.get('probe_journal_mode') == 'delete' and record.get('probe_synchronous') == 2 and record.get('wal_mode_qualified') is False, 'unqualified journal-mode change')
    rust = validate(record.get('rust', {}), 'rust')
    python = validate(record.get('python', {}), 'python')
    reader = record.get('qualification_reader', {})
    require(isinstance(reader.get('identity'), dict) and isinstance(reader['identity'].get('version'), str)
            and reader['identity'].get('source_id') and reader['identity'].get('compile_options'), 'missing qualification reader')
    require(reader.get('wal_reset_fixed') is wal_reset_fixed(reader['identity']['version']), 'inconsistent reader classification')
    fields = ('version', 'source_id', 'compile_options', 'binding', 'python_version',
              'python_executable_sha256', 'module_origin', 'extension_sha256',
              'journal_mode', 'synchronous', 'checks')
    clean = lambda value: {k: value[k] for k in fields if k in value}
    return dict(format_version=1, status='passed', release_identity=release_identity,
                policy_sha256=record['policy_sha256'], rust=clean(rust), python=clean(python),
                qualification_reader=dict(identity=clean(reader['identity']), wal_reset_fixed=reader['wal_reset_fixed']),
                probe_journal_mode='delete', probe_synchronous=2, wal_mode_qualified=False)
