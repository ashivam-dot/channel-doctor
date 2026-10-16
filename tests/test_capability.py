from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from doctor import fixer, heal, playbook, state, watch

CLEANUP = """2026-10-14T18:48:25.1Z Post job cleanup.
2026-10-14T18:48:25.2Z [command]/usr/bin/git version
2026-10-14T18:48:25.3Z git version 2.55.0
2026-10-14T18:48:25.4Z Temporarily overriding HOME='/home/runner/work/_temp/x' before making global git config changes
2026-10-14T18:48:25.5Z [command]/usr/bin/git config --local --unset-all http.https://github.com/.extraheader
2026-10-14T18:48:25.6Z ##[warning]Node.js 20 is deprecated. The following actions target Node.js 20: actions/cache@v4
2026-10-14T18:48:25.7Z Cleaning up orphan processes"""


def step(cmd, output):
    return (f"2026-10-14T18:48:24.0Z ##[group]Run {cmd}\n2026-10-14T18:48:24.1Z shell: /usr/bin/bash -e {{0}}\n"
            f"2026-10-14T18:48:24.2Z env:\n2026-10-14T18:48:24.3Z   PAUSED: false\n2026-10-14T18:48:24.4Z ##[endgroup]\n"
            + "".join(f"2026-10-14T18:48:24.5Z {line}\n" for line in output)
            + "2026-10-14T18:48:24.6Z ##[error]Process completed with exit code 1.\n")


def test_the_real_error_is_read_not_the_runner_cleanup():
    log = step("python -m studio.health", ["checking uploads", "no upload for 34 hours (limit 30.0)"]) + CLEANUP
    kind, evidence = playbook.classify(log)
    assert (kind, evidence) == ("unknown", "no upload for 34 hours (limit 30.0)")
    text = playbook.excerpt(log)
    assert "Run python -m studio.health" in text and "PAUSED" not in text and "orphan" not in text


def test_a_traceback_and_a_json_reason_are_both_found():
    trace = step("python -m scam_autopsy.cli publish", [
        "Traceback (most recent call last):",
        '  File "/home/runner/work/scam-autopsy/scam-autopsy/scam_autopsy/youtube.py", line 144, in finalize',
        '    raise RuntimeError("Privacy verification failed")',
        "RuntimeError: Privacy verification failed"])
    assert playbook.classify(trace + CLEANUP)[1] == "RuntimeError: Privacy verification failed"
    held = step("python -m lite.run", ['  "failed_topics": [', '   {', '    "topic": "ramayana-6-31",',
                                        '    "reason": "gate failed: speech_coverage 0.743"', '   }', '  ],',
                                        '  "status": "failed",', '}'])
    assert playbook.classify(held + CLEANUP)[1] == '"reason": "gate failed: speech_coverage 0.743"'


def test_known_failures_still_classify_inside_the_failing_step():
    log = step("python -m studio.cli run", ["googleapiclient.errors.HttpError: quotaExceeded"]) + CLEANUP
    assert playbook.classify(log)[0] == "quota"
    assert playbook.classify(CLEANUP, "timed_out")[0] == "transient"


def test_a_traceback_from_the_failed_runs_checkout_points_into_the_doctors_checkout(tmp_path):
    (tmp_path / "scam_autopsy").mkdir()
    (tmp_path / "scam_autopsy" / "youtube.py").write_text("x = 1\n")
    path = "/home/runner/work/scam-autopsy/scam-autopsy/scam_autopsy/youtube.py"
    assert fixer.in_repo(path, tmp_path) == "scam_autopsy/youtube.py"
    assert fixer.in_repo("/usr/lib/python3.12/site-packages/scam_autopsy/youtube.py", tmp_path) == "scam_autopsy/youtube.py"
    assert fixer.in_repo("/home/runner/.venv/lib/x.py", tmp_path) is None
    log = f'  File "{path}", line 144, in finalize'
    assert fixer.referenced(log, tmp_path) == {"scam_autopsy/youtube.py": [144]}


AT = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def vid(title, h):
    return {"title": title, "published": (AT - timedelta(hours=h)).isoformat(), "views": 1}


def test_visible_title_defects_are_caught_and_good_titles_pass():
    bad = ["places Where One City Holds 92% of City Dwellers", "61 places Still Have Most People Outside Cities",
           "The None of Rome", "Japan  Has 51 Seniors", "The the Last Train"]
    assert all(watch.title_problems(t) for t in bad)
    good = ["How the M6.6 Panama quake ruptured, in 3D", "The Path of Hurricane Isaias",
            "Japan Has 51 Seniors per 100 Working-Age People", "In Qatar, 77% of people were born abroad",
            "Health Spending: The $10,550 to $13 Divide", "Quiet mats of living silk #shorts",
            "The Work-From-Home Job That Charges You to Work", "बिल्ली ने पुरानी केतली दी… सुबह ज़मींदार का हाल देख सब चौंक गए! ☕",
            "Fall Asleep in the Coal Forests | 2 Hours of the Ancient Forests",
            "एलोन मस्क की कंपनी टेस्ला ने पेश की नई रोडस्टर स्पोर्ट्स कार",
            "वाल्मीकि रामायण के अनुसार राजा दशरथ से कौन सी बड़ी भूल हुई थी?"]
    assert [t for t in good if watch.title_problems(t)] == []


def test_quality_looks_at_recent_uploads_and_duplicates_only():
    ch = {"name": "C"}
    assert watch.quality(ch, [vid("places Where X Is", 100)], AT) is None
    assert "lower case" in watch.quality(ch, [vid("places Where X Is", 3)], AT)
    assert "uploaded 2 times" in watch.quality(ch, [vid("The Same Story", 3), vid("The same story", 5)], AT)
    assert watch.symptom({"name": "C", "quiet_h": 10}, [vid("places Where X Is", 3)], AT)[0] == "qa"
    assert watch.symptom({"name": "C", "quiet_h": 10, "hands_off": True}, [vid("places Where X Is", 3)], AT) is None
    assert watch.symptom({"name": "C", "quiet_h": 1}, [vid("places Where X Is", 3)], AT)[0] == "quiet"
    fixed_since = (AT - timedelta(hours=2)).isoformat()
    assert watch.quality(ch, [vid("places Where X Is", 3)], AT, fixed_since) is None
    assert "lower case" in watch.quality(ch, [vid("places Where X Is", 1)], AT, fixed_since)


def test_the_producing_codes_last_change_is_the_newest_editable_commit():
    class Commits:
        def get(self, path, **params):
            assert path.startswith("/repos/o/r/commits?") and not params
            return [{"commit": {"committer": {"date": {"a": "2026-10-16T14:38:42Z", "b": "2026-10-10T00:00:00Z"}[
                parse_qs(path.split("?", 1)[1])["path"][0]]}}}]
    assert watch.code_changed(Commits(), "o/r", {"editable": ["a/**/*.py", "b/*.py"]}) == "2026-10-16T14:38:42Z"
    assert watch.code_changed(Commits(), "o/r", {}) is None


class GH:
    def __init__(self):
        self.runs = {
            "41": {"id": 41, "html_url": "h", "name": "health", "path": ".github/workflows/health.yml", "run_attempt": 1,
                   "conclusion": "failure", "head_branch": "main", "head_sha": "abc"},
        }

    def get(self, path, **params):
        if path.endswith("/runs/41"):
            return self.runs["41"]
        if path.endswith("/runs/41/jobs"):
            return {"jobs": [{"id": 1, "name": "check", "conclusion": "failure",
                              "steps": [{"name": "Hours since the last upload", "conclusion": "failure"}]}]}
        if path.endswith("/runs/8/jobs"):
            return {"jobs": [{"id": 2, "name": "make", "conclusion": "success"}]}
        if path.endswith("/jobs/1/logs"):
            return step("python -m studio.health", ["no upload for 34 hours (limit 30.0)"]) + CLEANUP
        if path.endswith("/jobs/2/logs"):
            return "skip wk-1: gate: only 0 usable photos\nnothing eligible to publish"
        if path.endswith("/workflows/run.yml/runs"):
            return {"workflow_runs": [{"id": 9, "conclusion": "failure"},
                                      {"id": 8, "conclusion": "success", "name": "run", "html_url": "r", "run_attempt": 1}]}
        return {"default_branch": "main"}


def test_a_health_check_failing_on_silence_heals_the_producer_instead(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "feed", lambda gh, cid: [{"published": state.iso(state.now() - timedelta(hours=34)),
                                                         "title": "T", "views": 1}])
    h = heal.Healer.__new__(heal.Healer)
    h.repo, h.dry, h.gh, h.can_edit, h.events, h.fixes, h.target = "o/r", True, GH(), True, [], [], tmp_path
    h.ch = {"name": "C", "youtube": "UCx", "quiet_h": 30}
    h.lane = {"mode": "fix", "max_fixes_per_day": 2, "workflows": {"run.yml": {"name": "run", "max_h": 3},
                                                                  "health.yml": {"name": "health", "max_h": 9}}}
    h.brain = type("B", (), {"ready": False})()
    healed = []
    h.attempt = lambda rec, wf_name, wf_file, steps, log, task, url, default: healed.append((rec, wf_file, log))
    h.heal("41")
    (rec, wf_file, log), = healed
    assert rec["run"] == 8 and rec["kind"] == "silent" and wf_file == "run.yml" and "usable photos" in log
