import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("notify_failure", Path(__file__).resolve().parent.parent / "notify_failure.py")
nf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(nf)


def test_message_has_name_link_and_event():
    m = nf.message("Bot X", "extra", "https://run", "schedule")
    assert "Bot X" in m and "https://run" in m and "schedule" in m and "extra" in m


def test_main_posts_once_and_returns_zero(monkeypatch):
    sent = []
    monkeypatch.setattr(nf, "post", lambda hook, text: sent.append((hook, text)) or True)
    monkeypatch.setattr(nf.os.path, "exists", lambda p: False)
    assert nf.main(["Bot X"], {"DISCORD_WEBHOOK": "https://h", "RUN_URL": "https://run"}) == 0
    assert len(sent) == 1 and sent[0][0] == "https://h" and "https://run" in sent[0][1]


def test_main_without_webhook_is_quiet_and_zero(monkeypatch):
    monkeypatch.setattr(nf.os.path, "exists", lambda p: False)
    assert nf.main(["Bot X"], {}) == 0


def test_failed_post_never_raises_or_changes_exit(monkeypatch):
    monkeypatch.setattr(nf, "post", lambda hook, text: False)
    monkeypatch.setattr(nf.os.path, "exists", lambda p: False)
    assert nf.main(["Bot X"], {"DISCORD_WEBHOOK": "https://h"}) == 0


def test_post_swallows_network_errors():
    def opener(*a, **k):
        raise OSError("down")
    assert nf.post("https://h", "t", tries=2, sleep=lambda s: None, opener=opener) is False


def test_marker_suppresses_message(monkeypatch):
    sent = []
    monkeypatch.setattr(nf, "post", lambda hook, text: sent.append(1) or True)
    monkeypatch.setattr(nf.os.path, "exists", lambda p: True)
    assert nf.main(["Bot X"], {"DISCORD_WEBHOOK": "https://h"}) == 0 and sent == []
