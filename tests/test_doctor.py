import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from doctor import config, fixer, growth, playbook, telegram, watch
from doctor.__main__ import caller
from doctor.brain import _json, rank
from doctor.redact import redact

LANE = {"repo": "demo", "mode": "fix", "editable": ["desk/**/*.py"], "test": "python3 -m pytest -q tests",
        "max_changed_lines": 10, "max_files": 2}


# ---- config ------------------------------------------------------------------------------------------------
def test_channels_json_is_consistent():
    cfg = config.load()
    repos = [lane["repo"] for _, _, lane in config.lanes(cfg)]
    assert len(repos) == len(set(repos))
    for key, ch, lane in config.lanes(cfg):
        assert lane["mode"] in ("fix", "heal")
        if lane["mode"] == "fix":
            assert lane.get("test") and lane.get("editable"), key
        assert not (ch.get("hands_off") and lane["mode"] == "fix")
    assert cfg["channels"]["universe"]["hands_off"] and not cfg["channels"]["universe"]["lanes"]


@pytest.mark.parametrize("path,ok", [
    ("desk/plan.py", True), ("desk/sub/x.py", True), ("desk/plan.json", False), (".github/workflows/run.yml", False),
    ("tests/test_plan.py", False), ("desk/test_x.py", False), ("CHANNEL.md", False), ("../desk/plan.py", False),
    ("/desk/plan.py", True), ("studio/x.py", False), ("desk/conftest.py", False)])
def test_editable_paths(path, ok):
    assert config.editable(path, LANE) is ok


# ---- redaction ---------------------------------------------------------------------------------------------
def test_redact_strips_credentials():
    text = ("key AIza" + "x" * 35 + " tg 123456789:" + "A" * 35 + " ya29." + "b" * 30 +
            ' {"refresh_token": "1//abcdefghijklmnopqrstuvwxyz0123456"} ghp_' + "c" * 36)
    out = redact(text)
    for bad in ("AIza", "123456789:", "ya29.", "1//abc", "ghp_"):
        assert bad not in out
    assert "refresh_token" in out and out.count("[redacted]") >= 5
    assert redact("plain log line 42 ok") == "plain log line 42 ok"


# ---- playbook ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("line,kind", [
    ("The job was not started because recent account payments have failed", "billing"),
    ("google.auth.exceptions.RefreshError: ('invalid_grant: Token has been expired or revoked.')", "auth"),
    ("googleapiclient.errors.HttpError: <HttpError 403 ... quotaExceeded>", "quota"),
    ("urllib.error.URLError: <urlopen error [Errno -3] Temporary failure in name resolution>", "transient"),
    ("KeyError: 'title'", "unknown")])
def test_classify(line, kind):
    log = "2026-10-10T08:00:00.1234567Z step\n" + "noise\n" * 5 + f"2026-10-10T08:00:01.0000000Z {line}\n"
    assert playbook.classify(log)[0] == kind


def test_classify_timeout_is_transient_and_fingerprint_is_stable():
    assert playbook.classify("all fine", "timed_out")[0] == "transient"
    a = playbook.fingerprint("run", "KeyError at line 41 in 9f3e2a1b")
    b = playbook.fingerprint("run", "KeyError at line 97 in 0c0ffee12")
    assert a == b and a != playbook.fingerprint("tests", "KeyError at line 41")


def test_first_error_prefers_the_real_error():
    log = "Traceback (most recent call last):\n  File \"desk/plan.py\", line 3\nValueError: bad story\nError: Process completed with exit code 1."
    assert playbook.first_error(log) == "ValueError: bad story"


# ---- brain -------------------------------------------------------------------------------------------------
def test_rank_prefers_newest_generation_with_tier_bonus():
    names = ["models/gemini-2.5-flash", "models/gemini-2.5-pro", "models/gemini-3.1-pro-preview",
             "models/gemini-3.8-flash", "models/gemini-2.5-flash-lite", "models/gemini-embedding-001",
             "models/gemini-2.5-flash-preview-tts", "models/gemma-3-27b-it"]
    assert rank(names, "deep")[:3] == ["models/gemini-3.8-flash", "models/gemini-3.1-pro-preview", "models/gemini-2.5-pro"]
    assert rank(names, "fast")[0] == "models/gemini-3.8-flash"
    assert rank(["models/gemini-3.8-flash", "models/gemini-3.8-pro"], "deep")[0] == "models/gemini-3.8-pro"
    assert "models/gemini-2.5-flash-preview-tts" not in rank(names)


def test_json_parsing_tolerates_fences():
    assert _json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _json('sure: {"a": 2} done') == {"a": 2}


def test_telegram_chunks_respect_limit():
    parts = telegram.chunks("line\n" * 2000 + "x" * 9000, 1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("line") == 2000


# ---- fixer -------------------------------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    root = tmp_path / "work"
    (root / "desk").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "desk/__init__.py").write_text("")
    (root / "desk/plan.py").write_text("def gap():\n    return 30\n\n\ndef cap():\n    return 10\n")
    (root / "tests/test_plan.py").write_text("from desk.plan import gap\n\n\ndef test_gap():\n    assert gap() == 15\n")
    for cmd in (["init", "-q", "-b", "main"], ["add", "."], [*fixer.BOT, "commit", "-qm", "init"],
                ["remote", "add", "origin", str(remote)], ["push", "-q", "origin", "main"]):
        subprocess.run(["git", *cmd], cwd=root, check=True)
    return root


def test_validate_rejects_unsafe_plans(repo):
    good = {"fixable_in_code": True, "confidence": 0.9,
            "edits": [{"path": "desk/plan.py", "find": "return 30", "replace": "return 15"}]}
    staged, why = fixer.validate(good, LANE, repo)
    assert why is None and staged[0]["path"] == "desk/plan.py"
    cases = [
        ({**good, "confidence": 0.3}, "confidence"),
        ({**good, "fixable_in_code": False}, "not fixable"),
        ({**good, "edits": [{"path": "tests/test_plan.py", "find": "== 15", "replace": "== 30"}]}, "outside"),
        ({**good, "edits": [{"path": "desk/plan.py", "find": "return", "replace": "yield"}]}, "2 times"),
        ({**good, "edits": [{"path": "desk/plan.py", "find": "return 30", "replace": "return 15\n" + "x = 1\n" * 20}]}, "limit"),
        ({**good, "edits": [{"path": "desk/none.py", "find": "a", "replace": "b"}]}, "does not exist"),
        ("garbage", "no usable answer"),
    ]
    for plan, expect in cases:
        assert expect in fixer.validate(plan, LANE, repo)[1]


def test_patch_test_commit_and_revert(repo):
    staged, _ = fixer.validate({"fixable_in_code": True, "confidence": 0.9,
                                "edits": [{"path": "desk/plan.py", "find": "return 30", "replace": "return 15"}]}, LANE, repo)
    ok, out = fixer.run_tests(repo, LANE)
    assert not ok
    fixer.apply(staged, repo)
    (repo / "scratch.txt").write_text("left by tests")
    ok, out = fixer.run_tests(repo, LANE)
    assert ok, out
    sha = fixer.commit_push(repo, "doctor: shorten gap", ["desk/plan.py"])
    shown = fixer.git(repo, "show", "--stat", "--format=%an|%s", sha)
    assert "github-actions[bot]|doctor: shorten gap" in shown and "scratch.txt" not in shown
    assert fixer.git(repo, "ls-remote", "origin", "main").startswith(sha)
    back = fixer.revert(repo, sha, "run still failing")
    assert "return 30" in (repo / "desk/plan.py").read_text()
    assert "doctor: revert" in fixer.git(repo, "log", "-1", "--format=%s", back)


def test_context_includes_traceback_files_and_marks_editability(repo):
    log = 'Traceback:\n  File "/home/runner/work/demo/demo/desk/plan.py", line 2, in gap\nAssertionError'
    ctx = fixer.context(repo, LANE, log, None)
    assert "## desk/plan.py (EDITABLE)" in ctx and "def gap" in ctx


# ---- watch -------------------------------------------------------------------------------------------------
class FakeGH:
    def __init__(self, routes, feeds):
        self.routes, self.feeds = routes, feeds

    def get(self, path, **params):
        for prefix, value in self.routes.items():
            if path.endswith(prefix):
                return value
        import urllib.error
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)

    def text(self, url):
        return self.feeds.get(url.split("=")[-1])


def atom(published):
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns:media="http://search.yahoo.com/mrss/"><entry><yt:videoId>v1</yt:videoId><title>T</title>'
            f'<published>{published}</published><media:group><media:community><media:statistics views="42"/>'
            f'</media:community></media:group></entry></feed>')


def test_watch_flags_quiet_late_streak_and_alerts():
    at = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    old = (at - timedelta(hours=40)).isoformat()
    fresh = (at - timedelta(hours=1)).isoformat()
    cfg = {"owner": "o", "channels": {
        "a": {"name": "A", "youtube": "UCa", "quiet_h": 18, "lanes": [
            {"repo": "ra", "mode": "fix", "workflows": {"run.yml": {"name": "run", "max_h": 2}}}]},
        "b": {"name": "B", "youtube": "UCb", "quiet_h": 18, "lanes": []}}}
    fail = {"status": "completed", "conclusion": "failure", "event": "schedule", "created_at": old,
            "updated_at": old, "html_url": "u"}
    gh = FakeGH({"/ra/actions/workflows/run.yml": {"state": "active"},
                 "/ra/actions/workflows/run.yml/runs": {"workflow_runs": [fail] * 3},
                 "/ra/actions/workflows/doctor.yml/runs": {"workflow_runs": []},
                 "/ra/issues": [{"number": 7, "title": "run silent", "html_url": "i"}]},
                {"UCa": atom(old), "UCb": atom(fresh)})
    problems, status = watch.check(gh, cfg, at)
    assert set(problems) == {"quiet:a", "late:ra/run.yml", "streak:ra/run.yml", "issue:ra#7"}
    assert "42 views" in " ".join(status["A"]) and "quiet:b" not in problems
    text = watch.digest(status, problems, at)
    assert "A [attention]" in text and "B [ok]" in text


def test_review_counts_only_videos_with_a_day_of_data():
    from doctor.review import scored
    at = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    vids = [{"views": 5, "published": (at - timedelta(hours=h)).isoformat()} for h in (2, 23, 25, 90)]
    vids.append({"views": None, "published": (at - timedelta(hours=50)).isoformat()})
    assert scored(vids, at) == 2


# ---- caller ------------------------------------------------------------------------------------------------
def test_caller_lists_watched_workflows():
    text = caller("hindi-news-desk")
    assert 'workflows: ["run", "health", "tests", "reference"]' in text
    assert "ashivam-dot/channel-doctor/.github/workflows/doctor.yml@main" in text
    assert "${{ format('{0}', inputs.run_id || github.event.workflow_run.id) }}" in text
    assert "vars.DOCTOR_PAUSED != 'true'" in text
    assert text.index("concurrency:") > text.index("  doctor:")
    assert "group: doctor-${{ github.event.workflow_run.path || github.event.schedule || github.event_name }}" in text
    heal_only = caller("history-last-hours-control")
    assert '"Atlas in Numbers publisher"' in heal_only and "cron" not in heal_only
    assert '- cron: "23 4 * * 2,5"' in text and '- cron: "41 14 * * *"' in text
    assert "github.event.schedule == '41 14 * * *' && 'growth' || 'improve'" in text
    assert "options: [heal, improve, growth]" in text
    assert '"41 14' not in caller("doctor-drill")


# ---- growth -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,n", [("43", 43), ("1.2K", 1200), ("12,345", 12345), ("3M", 3_000_000), ("", None), ("x", None)])
def test_count(text, n):
    assert growth.count(text) == n


def test_listed_reads_ids_and_views():
    html = ('{"entityId":"shorts-shelf-item-x","videoId": "abcdefghijk", "accessibilityText": "Volcano, 1.2K views - play Short"}'
            '{"contentId": "ABCDEFGHIJK", "content": "659 views"}')
    assert growth.listed(html) == {"abcdefghijk": 1200, "ABCDEFGHIJK": 659}


def test_rate_and_eta():
    hist = [["2026-10-01", 10, None, None], ["2026-10-06", 20, None, None], ["2026-10-11", 40, None, None]]
    assert growth.rate(hist, 1) == pytest.approx(3.0)
    assert growth.rate(hist[:1], 1) is None
    assert growth.eta_days(40, 500, 3.0) == pytest.approx(460 / 3)
    assert growth.eta_days(600, 500, None) == 0 and growth.eta_days(40, 500, 0) is None


def test_road_line_and_merge():
    at = datetime(2026, 10, 11, tzinfo=timezone.utc)
    fresh = {"subs": 120, "shorts_views_90d": 50_000, "long_hours_365d": 2.5, "subs_net_28d": 56, "at": at.isoformat()}
    m = growth.merged({"subs": 118, "shorts_views_listed": 9000}, fresh, at)
    assert m["subs"] == 120 and m["exact"] is True
    stale = dict(fresh, at=(at - timedelta(days=3)).isoformat())
    assert growth.merged({"subs": 118, "shorts_views_listed": 9000}, stale, at)["exact"] is False
    line, info = growth.road("Kyun Hua", m, [], at.date(), "shorts")
    assert "120 subs" in line and "Shorts views 90d 50,000" in line and info["subs_per_day"] == pytest.approx(2.0)
    assert info["expanded_sub_eta_days"] == pytest.approx(190) and info["days_to_deadline"] == 112
    assert "no subscriber number" in growth.road("X", {"subs": None}, [], at.date(), "shorts")[0]


def test_review_road_line_conversion():
    from doctor.review import road_line
    info = {"subs": 43, "route": "shorts", "days_to_deadline": 112, "subs_per_day": None, "exact": False,
            "public": {"shorts_views_listed": 16496, "long_views_listed": 0}}
    assert "2.6 subscribers per 1,000 views" in road_line(info)
    assert road_line({}) == "no numbers yet"


def test_mission_loaded():
    from doctor import mission
    assert "31 January 2027" in mission.brief() and "inauthentic" in mission.brief()
