import hashlib
import json
from pathlib import Path

from apex_chronos import alerts


def test_default_alerts_match_frozen_production_bytes(fast_run):
    texts = []
    for day, event in fast_run[2]:
        if event.kind == 'ENTRY':
            text = alerts.fmt_entry(day, event, 1500, .25, 7, confidence='FROZEN CONFIDENCE')
        elif event.kind == 'TP1':
            text = alerts.fmt_tp1(day, event)
        elif event.kind == 'EXIT':
            text = alerts.fmt_exit(day, event, capital=1500, slot_fraction=.25)
        else:
            text = alerts.fmt_skip(day, event)
        texts.append(text)
    frozen = json.loads((Path(__file__).parent / 'golden/legacy-alerts.json').read_text())
    assert len(texts) == frozen['messages']
    assert hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest() == frozen['sha256']
