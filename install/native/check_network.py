#!/usr/bin/env python3
"""Reject attempted non-loopback network destinations in Linux strace evidence."""
import argparse
import ipaddress
import json
from pathlib import Path
import re


def inspect(trace):
    calls = 0
    violations = set()
    with trace.open(errors='replace') as stream:
        for line in stream:
            if not re.search(r'\b(connect|sendto|sendmsg|sendmmsg)\(', line):
                continue
            for address in re.findall(r'inet_addr\("([^"]+)"\)|inet_pton\(AF_INET6, "([^"]+)"', line):
                calls += 1
                value = next(a for a in address if a)
                address = ipaddress.ip_address(value)
                if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                    address = address.ipv4_mapped
                if not address.is_loopback:
                    violations.add(value)
    if calls == 0:
        raise ValueError('no network destination evidence; trace is incomplete')
    result = dict(status='failed' if violations else 'passed', observed_destinations=calls,
                  external_destinations=sorted(set(violations)), scope='traced harness and all descendant processes; connect/sendto/sendmsg/sendmmsg')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    result = inspect(args.trace)
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
