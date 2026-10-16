from datetime import timedelta
from pathlib import Path

from doctor import heal, state, watch


class GH:
    def __init__(self, conclusion):
        self.run = {"id": 5, "html_url": "u", "name": "run", "path": ".github/workflows/run.yml", "run_attempt": 1,
                    "conclusion": conclusion, "head_branch": "main", "head_sha": "abc"}

    def get(self, path, **params):
        if path.endswith("/runs/5"):
            return self.run
        if path.endswith("/jobs"):
            return {"jobs": [{"id": 1, "name": "make"}]}
        if path.endswith("/jobs/1/logs"):
            return "hold: the buffer is empty; nothing eligible"
        return {"default_branch": "main"}


def healer(gh, can_edit):
    h = heal.Healer.__new__(heal.Healer)
    h.repo, h.dry, h.gh, h.can_edit, h.events, h.fixes = "o/r", True, gh, can_edit, [], []
    h.target, h.brain = Path("."), type("B", (), {"ready": False})()
    h.ch = {"name": "C", "youtube": "UCx", "quiet_h": 10}
    h.lane = {"mode": "fix", "max_fixes_per_day": 2, "workflows": {"run.yml": {"name": "run", "max_h": 3}}}
    h.finished, h.attempted = [], []
    h.finish = lambda rec, action, msg, notify, url="": h.finished.append((rec, action, notify))
    h.attempt = lambda rec, *a, **k: h.attempted.append((rec, a))
    h.diagnose = lambda *a: {"diagnosis": "the buffer is empty"}
    return h


def test_a_green_run_of_a_silent_channel_is_healed_and_a_green_run_of_a_live_one_is_not(monkeypatch):
    last = {"t": state.now() - timedelta(hours=30)}
    monkeypatch.setattr(watch, "feed", lambda gh, cid: [{"published": state.iso(last["t"]), "title": "T", "views": 1}])
    h = healer(GH("success"), can_edit=True)
    h.heal("5")
    (rec, args), = h.attempted
    assert rec["kind"] == "silent" and "no new public upload for 30 h" in rec["evidence"]
    assert "SUCCEEDED" in args[4] and "buffer is empty" in args[3]

    last["t"] = state.now() - timedelta(hours=1)
    h = healer(GH("success"), can_edit=True)
    h.heal("5")
    assert h.attempted == [] and h.finished == []


def test_a_report_only_lane_explains_the_silence_once_a_day(monkeypatch):
    monkeypatch.setattr(watch, "feed", lambda gh, cid: [{"published": state.iso(state.now() - timedelta(hours=30)),
                                                         "title": "T", "views": 1}])
    h = healer(GH("success"), can_edit=False)
    h.heal("5")
    (rec, action, notify), = h.finished
    assert action == "escalated" and notify == "SILENT" and h.attempted == []
    h.events.append(dict(rec, action="escalated", run=6))
    h.finished.clear()
    h.heal("5")
    assert h.finished[0][2] is None
