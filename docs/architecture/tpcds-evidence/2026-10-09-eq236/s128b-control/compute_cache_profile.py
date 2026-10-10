"""Select the public creation-time cache profile without changing frozen COPY code."""
import subprocess
from unittest.mock import patch


def install(cell, profile):
    if profile not in ('compact', 'source-load'):
        raise ValueError('unknown compute cache profile')
    original_start = cell.start

    def start():
        original_popen = subprocess.Popen

        def launch(argv, *args, **kwargs):
            if list(argv[:2]) == [str(cell.binary), 'daemon']:
                argv = [*argv, '--compute-cache-profile', profile]
            return original_popen(argv, *args, **kwargs)

        with patch('subprocess.Popen', launch):
            result = original_start()
        assert cell.config['compute_cache_profile'] == profile
        return result

    cell.start = start
