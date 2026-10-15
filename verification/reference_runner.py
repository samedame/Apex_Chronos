"""Run the ORIGINAL backtest code (verification/reference/*.py) on any data folder.

Only mechanical edits are applied to a temporary copy: the hard-coded data/output
paths and (optionally) the TICKERS list. Returns everything needed to compare:
  df_c     every candidate the original generated, before slot scheduling
  df_exec  the final scheduled trade ledger
  all_data the per-ticker, per-day feature arrays the original computed
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, 'reference')
FILES = ['run_1m_options_lab.py', 'fast_sim.py', 'analyze_chronos_portfolio.py', 'run_apex_chronos_final.py']
MODS = ['run_1m_options_lab', 'fast_sim', 'analyze_chronos_portfolio', 'run_apex_chronos_final']


def check_hashes():
    """Verify the oracle files match the pinned SHA-256 sums."""
    bad = []
    with open(os.path.join(REF, 'SHA256SUMS')) as fh:
        for line in fh:
            h, name = line.split()
            with open(os.path.join(REF, name), 'rb') as f:
                if hashlib.sha256(f.read()).hexdigest() != h:
                    bad.append(name)
    return bad


def run_reference(data_dir: str, tickers=None, quiet: bool = True):
    tmp = tempfile.mkdtemp(prefix='apex_ref_')
    out_csv = os.path.join(tmp, 'oracle_trades.csv')
    try:
        for f in FILES:
            src = open(os.path.join(REF, f), encoding='utf-8').read()
            src = src.replace('/home/user/data', data_dir.rstrip('/'))
            src = src.replace('/home/user/apex_chronos_final_trades.csv', out_csv)
            if tickers is not None and f == 'run_1m_options_lab.py':
                src, n = re.subn(r"^TICKERS = \[.*?\]$", "TICKERS = " + repr(list(tickers)), src, count=1, flags=re.M)
                assert n == 1, 'TICKERS line not found'
            with open(os.path.join(tmp, f), 'w', encoding='utf-8') as fh:
                fh.write(src)
        for m in MODS:
            sys.modules.pop(m, None)
        sys.path.insert(0, tmp)
        buf = io.StringIO()
        try:
            ctx = contextlib.redirect_stdout(buf) if quiet else contextlib.nullcontext()
            with ctx:
                lab = importlib.import_module('run_1m_options_lab')
                final = importlib.import_module('run_apex_chronos_final')
        finally:
            sys.path.remove(tmp)
        result = {
            'df_c': final.df_c.copy(),
            'df_exec': final.df_exec.copy(),
            'all_data': lab.ALL_DATA,
            'log': buf.getvalue(),
        }
        for m in MODS:
            sys.modules.pop(m, None)
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
