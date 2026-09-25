#!/usr/bin/env python3
"""Reproduce a comparison from retained reports without launching a runtime."""
import argparse
from compare import comparison_report
from comparison import read_json

if __name__ == '__main__':
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    report = comparison_report(args.root, read_json(args.root/'experiment.json'))
    print('Complete:', report['complete'], 'accepted pairs:', report['accepted_pairs'])
