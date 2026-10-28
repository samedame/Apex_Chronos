"""Reproduce every number in REPORT.md:  python research/confidence_backtest/run_all.py   (about 6 minutes, offline)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for script in ('dataset.py', 'analysis_q1q2.py', 'stress.py', 'diagnostics.py'):
    print(f'--- {script}', flush=True)
    subprocess.run([sys.executable, os.path.join(HERE, script)], check=True)
