"""Explicit workload admission; the original input lock remains unchanged."""
import json
from pathlib import Path

from inputs import sha


def require_storage(profile, free_bytes, cell_bytes=0, *, admission=False):
    bounds = profile['load_bounds']
    minimum = bounds['minimum_free_gib'] if admission else bounds['minimum_remaining_free_gib']
    if free_bytes < minimum * 1024**3 or cell_bytes > bounds['maximum_cell_gib'] * 1024**3:
        raise RuntimeError('workload storage admission/safety bound reached')


def workload(name):
    if name == 'sf1':
        return dict(name='eq02-sf1', scale=1, business_rows=19557335,
                    storage_profile='compact', profile_sha256=None,
                    load_bounds=dict(timeout_seconds=7200, minimum_free_gib=80,
                                     maximum_cell_gib=64, minimum_remaining_free_gib=16,
                                     storage_sample_seconds=10, default_max_unpublished_rows=0))
    if name != 'sf100':
        raise ValueError('unknown workload')
    path = Path(__file__).with_name('workload-sf100.json')
    result = json.loads(path.read_text())
    result['profile_sha256'] = sha(path)
    if sum(result['storage_budget_gib'].values()) != result['load_bounds']['minimum_free_gib']:
        raise ValueError('inconsistent workload storage budget')
    return result


def validate_generation(profile, generation, receipt_sha256, lock_sha256, tables):
    if generation['status'] != 'PASS' or generation['input_lock_sha256'] != lock_sha256:
        raise ValueError('generation is not a validated pinned input')
    if generation.get('scale', 1) != profile['scale'] or generation['business_rows'] != profile['business_rows']:
        raise ValueError('generation scale/row count differs from workload')
    if profile['scale'] != 1:
        if receipt_sha256 != profile['generation_receipt_sha256']:
            raise ValueError('generation receipt differs from workload pin')
        if generation.get('generation_profile', {}).get('sha256') != profile['generation_profile_sha256']:
            raise ValueError('generation profile differs from workload pin')
    expected = {t['file']: t for t in generation['tables']}
    if len(expected) != len(generation['tables']) or set(expected) != {t['name']+'.dat' for t in tables}:
        raise ValueError('generation table inventory differs')
    if sum(t['rows'] for t in expected.values()) != profile['business_rows']:
        raise ValueError('generation table row counts do not sum to workload total')
    return expected
