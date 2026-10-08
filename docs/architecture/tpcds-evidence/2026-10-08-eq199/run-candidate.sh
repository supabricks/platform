#!/bin/bash
set -eu
python3 build/eq199/package.py > build/eq199/package.log 2>&1
bash build/eq199/run-upgrade.sh
bash build/eq199/run-reduced.sh candidate
python3 build/eq199/compare-reduced.py > build/eq199/reduced-comparison.log 2>&1
bash build/eq199/run-sql-tests.sh candidate
bash build/eq199/run-sql-tests.sh cluster
bash build/eq199/run-qualification.sh candidate
