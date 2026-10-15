"""Persistence: sent-alert ledger (dedupe across restarts), model trade ledger, git commit helper."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from datetime import date
from typing import Dict, Iterable, List

LEDGER_COLUMNS = ['ticker', 'date', 'm_entry', 'm_exit', 'hold_mins', 'direction', 'cost_basis', 'roi', 'peak_roi',
                  'exit_reason', 'rvol_entry', 'module', 'phase', 'score', 'slot']


def sent_path(state_dir: str, d: date) -> str:
    return os.path.join(state_dir, f'sent-{d.isoformat()}.json')


def load_sent(state_dir: str, d: date) -> Dict[str, dict]:
    p = sent_path(state_dir, d)
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_sent(state_dir: str, d: date, sent: Dict[str, dict]) -> None:
    os.makedirs(state_dir, exist_ok=True)
    tmp = sent_path(state_dir, d) + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(sent, fh, indent=1, sort_keys=True)
    os.replace(tmp, sent_path(state_dir, d))


def read_ledger(ledger_dir: str) -> List[dict]:
    p = os.path.join(ledger_dir, 'trades.csv')
    if not os.path.exists(p):
        return []
    with open(p, newline='') as fh:
        return list(csv.DictReader(fh))


def replace_day_in_ledger(ledger_dir: str, d: date, trades: Iterable[dict]) -> None:
    """The ledger holds the model trades from the FINAL-bar replay of each session (one block per date)."""
    os.makedirs(ledger_dir, exist_ok=True)
    rows = [r for r in read_ledger(ledger_dir) if r['date'] != d.isoformat()]
    for t in trades:
        rows.append({c: t[c] for c in LEDGER_COLUMNS})
    rows.sort(key=lambda r: (r['date'], int(r['m_entry']), r['ticker']))
    p = os.path.join(ledger_dir, 'trades.csv')
    tmp = p + '.tmp'
    with open(tmp, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=LEDGER_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, p)


def git_commit_push(paths: List[str], message: str, retries: int = 4) -> bool:
    """Commit and push state files (GitHub Actions only: enabled by APEX_GIT_PUSH=1). Never raises."""
    if os.environ.get('APEX_GIT_PUSH') != '1':
        return False
    try:
        subprocess.run(['git', 'config', 'user.name', 'apex-chronos-bot'], check=True)
        subprocess.run(['git', 'config', 'user.email', 'apex-chronos-bot@users.noreply.github.com'], check=True)
        subprocess.run(['git', 'add', '--'] + paths, check=True)
        if subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode == 0:
            return True  # nothing to commit
        subprocess.run(['git', 'commit', '-m', f'{message} [skip ci]'], check=True, capture_output=True)
        for i in range(retries):
            r = subprocess.run(['git', 'push'], capture_output=True, text=True)
            if r.returncode == 0:
                return True
            pull = subprocess.run(['git', 'pull', '--rebase', '--autostash'], capture_output=True, text=True)
            if pull.returncode != 0:
                subprocess.run(['git', 'rebase', '--abort'], capture_output=True)   # never leave the checkout mid-rebase
            time.sleep(1.5 * (i + 1))
    except (subprocess.CalledProcessError, OSError) as e:
        print(f'git push failed: {e!r}')
    return False
