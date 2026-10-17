from datetime import datetime, timedelta, timezone

from doctor import brain as brain_mod
from doctor import lab, local, mission, tidy

AT = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def ago(h):
    return (AT - timedelta(hours=h)).isoformat().replace("+00:00", "Z")


# ---- breakouts -------------------------------------------------------------------------------------------
def feed_vid(i, h, views):
    return {"id": f"v{i}", "title": f"Video {i}", "published": ago(h), "views": views}


def test_a_fresh_upload_at_three_times_the_usual_is_a_breakout_once():
    vids = {"Kyun Hua": [feed_vid(0, 7, 1250), feed_vid(1, 9, 40)] + [feed_vid(i, 50 + i, 100 + i) for i in range(2, 8)]}
    seen = {}
    msgs = lab.breakouts(vids, AT, seen)
    assert len(msgs) == 1 and "BREAKOUT Kyun Hua" in msgs[0] and "1,250 views" in msgs[0] and "youtu.be/v0" in msgs[0]
    assert lab.breakouts(vids, AT, seen) == [] and seen["v0"]["x"] > 3


def test_small_numbers_and_thin_history_are_not_breakouts():
    tiny = {"Mool": [feed_vid(0, 3, 54)] + [feed_vid(i, 60, 14) for i in range(1, 8)]}
    thin = {"New": [feed_vid(0, 3, 900), feed_vid(1, 60, 10)]}
    assert lab.breakouts(tiny, AT, {}) == [] and lab.breakouts(thin, AT, {}) == []


# ---- experiments -----------------------------------------------------------------------------------------
def yt_vid(i, h, views, title=None, **extra):
    return {"id": f"y{i}", "snippet": {"title": title or f"Story {i} #shorts", "description": "Para one.\n\nCredits: CC BY",
                                       "categoryId": "24", "tags": ["a"], "publishedAt": ago(h)},
            "statistics": {"viewCount": str(views)}, "status": {"privacyStatus": "public"}, **extra}


class FakeYT:
    def __init__(self, vids, scopes=("youtube.force-ssl", "yt-analytics.readonly"), fail=None):
        self.vids, self.scopes, self.calls, self.fail, self.posted = vids, list(scopes), [], fail, set()
        self.units = 0

    @property
    def can_write(self):
        return "youtube.force-ssl" in self.scopes

    def uploads(self):
        return self.vids

    def commented(self, video_id):
        return video_id in self.posted

    def call(self, method, path, params, body=None, cost=1):
        self.calls.append((method, path, params, body))
        if self.fail == path:
            raise TimeoutError("slow")
        if path == "commentThreads":
            self.posted.add(body["snippet"]["videoId"])
        return {}


class FakeBrain:
    ready, used = True, "gemini-x"

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def ask_json(self, prompt, *a, **k):
        self.prompts.append(prompt)
        for key, value in self.answers.items():
            if key in prompt:
                return value
        return None


CH = {"name": "Kahaani Chaos", "about": "Hindi twist stories", "lab": {"language": "hi", "experiments": ["comment"]}}


def test_a_new_upload_gets_one_clean_first_comment_and_never_twice():
    yt = FakeYT([yt_vid(1, 5, 30), yt_vid(2, 80, 30)])
    b = FakeBrain({"first comment": {"comment": "आप होते तो केतली रखते या लौटा देते?"}})
    state = {}
    log = lab.run_experiments(yt, b, CH, state, AT)
    posts = [c for c in yt.calls if c[1] == "commentThreads"]
    assert len(posts) == 1 and posts[0][3]["snippet"]["videoId"] == "y1" and "Hindi" in b.prompts[0]
    assert "केतली" in log[0] and "y1" in state["done"]["comment"]
    lab.run_experiments(yt, b, CH, state, AT)
    assert len([c for c in yt.calls if c[1] == "commentThreads"]) == 1
    lost = {}
    lab.run_experiments(yt, b, CH, lost, AT)
    assert len([c for c in yt.calls if c[1] == "commentThreads"]) == 1 and lost["done"]["comment"]["y1"]["found"]
    assert "text" not in state["done"]["comment"]["y1"]


def comment(cid, text, author="UCviewer", h=3):
    return {"id": cid, "snippet": {"textDisplay": text, "authorChannelId": {"value": author}, "publishedAt": ago(h)}}


def thread(top, replies=(), vid="y1"):
    return {"id": top["id"], "snippet": {"videoId": vid, "topLevelComment": top, "totalReplyCount": len(replies)},
            "replies": {"comments": list(replies)}}


class ThreadYT(FakeYT):
    channel_id = "UCme"

    def __init__(self, threads, **k):
        super().__init__([yt_vid(1, 5, 30, title="Kolkata pandal stampede")], **k)
        self.all = threads

    def threads(self):
        return self.all

    def replies(self, thread_id):
        return []


def test_each_positive_comment_gets_one_warm_reply_and_nothing_else_is_answered():
    praise, critic, spam = comment("c1", "बहुत बढ़िया जानकारी, धन्यवाद"), comment("c2", "galat news hai"), \
        comment("c3", "sub to my channel")
    answered = comment("c4", "nice video")
    ours = comment("c5", "आप क्या सोचते हैं?", author="UCme", h=10)
    later = comment("c6", "Great channel bro", h=2)
    yt = ThreadYT([thread(praise), thread(critic), thread(spam),
                   thread(answered, [comment("r1", "धन्यवाद!", author="UCme")]), thread(ours, [later])])
    b = FakeBrain({"POSITIVE": {"replies": [{"n": 1, "positive": True, "reply": "धन्यवाद, जुड़े रहिए 🙏"},
                                            {"n": 2, "positive": False}, {"n": 3, "positive": False},
                                            {"n": 4, "positive": True, "reply": "Thanks a lot, glad you like it!"}]}})
    ch = CH | {"lab": {"language": "hi", "experiments": ["reply"]}}
    state = {}
    log = lab.run_experiments(yt, b, ch, state, AT)
    posts = [c for c in yt.calls if c[1] == "comments"]
    assert [p[3]["snippet"]["parentId"] for p in posts] == ["c1", "c5"] and len(log) == 2
    assert "c4" not in b.prompts[0] and "nice video" not in b.prompts[0] and "Kolkata" in b.prompts[0]
    assert state["done"]["reply"]["c2"]["positive"] is False and state["done"]["reply"]["c6"]["positive"]
    lab.run_experiments(yt, b, ch, state, AT)
    assert len([c for c in yt.calls if c[1] == "comments"]) == 2


def test_a_reply_with_a_link_or_a_subscribe_request_is_never_posted():
    yt = ThreadYT([thread(comment("c1", "love it"))])
    b = FakeBrain({"POSITIVE": {"replies": [{"n": 1, "positive": True, "reply": "Thanks! Subscribe for more"}]}})
    log = lab.run_experiments(yt, b, CH | {"lab": {"language": "en", "experiments": ["reply"]}}, {}, AT)
    assert log == [] and not [c for c in yt.calls if c[1] == "comments"]


def test_a_failed_write_is_recorded_and_not_retried_every_day():
    ch = {"name": "Orbitwire", "about": "", "lab": {"language": "en", "experiments": ["comment", "localize"],
                                                     "languages": ["es"]}}
    b = FakeBrain({"first comment": {"comment": "Which part surprised you?"},
                   "Spanish": {"title": "Terremoto #shorts", "summary": "Uno."}})
    yt, state = FakeYT([yt_vid(1, 5, 30)], fail="videos"), {}
    log = lab.run_experiments(yt, b, ch, state, AT)
    assert state["done"]["localize"]["y1"]["error"] == "TimeoutError" and any("refused" in l for l in log)
    assert "y1" in state["done"]["comment"]
    lab.run_experiments(yt, b, ch, state, AT)
    assert len([c for c in yt.calls if c[1] == "videos"]) == 1


def test_the_lab_state_saves_even_when_a_run_crashes(monkeypatch):
    import pytest
    from doctor import config, state as state_mod
    saved = []
    monkeypatch.setenv("YOUTUBE_OAUTH_JSON", "{}")
    monkeypatch.setattr(config, "lane_for", lambda cfg, repo: (None, CH, {}))
    monkeypatch.setattr(lab, "YouTube", lambda raw: FakeYT([]))
    monkeypatch.setattr(state_mod, "IssueState", type("S", (), {"__init__": lambda self, *a: setattr(self, "data", {}),
                                                                 "save": lambda self: saved.append(dict(self.data))}))
    monkeypatch.setattr("doctor.github.GitHub", lambda: None)
    monkeypatch.setattr(lab, "run_experiments", lambda *a: (_ for _ in ()).throw(TimeoutError("api")))
    with pytest.raises(TimeoutError):
        lab.run_repo("o/r", {})
    assert saved and saved[0]["scopes"]


def test_old_entries_are_pruned_so_the_state_issue_stays_small():
    state = {"done": {"comment": {"old": {"at": (AT - timedelta(days=9)).isoformat()},
                                  "new": {"at": (AT - timedelta(days=1)).isoformat()}}},
             "retitles": [{"id": str(i)} for i in range(40)]}
    lab.prune(state, AT)
    assert list(state["done"]["comment"]) == ["new"] and len(state["retitles"]) == 30


def test_a_breakout_without_a_title_does_not_crash_the_watch():
    vids = {"K": [dict(feed_vid(0, 7, 1250), title=None)] + [feed_vid(i, 50 + i, 100) for i in range(1, 6)]}
    assert "BREAKOUT K" in lab.breakouts(vids, AT, {})[0]


def test_spammy_comments_are_refused():
    for text in ("Like and subscribe!", "Watch more at https://x.y", "Follow @me", "Best story #viral", "x" * 300):
        assert lab.clean_comment(text) is None
    assert lab.clean_comment("Which twist did you see coming?") == "Which twist did you see coming?"


def test_without_the_write_scope_nothing_is_touched():
    yt = FakeYT([yt_vid(1, 5, 30)], scopes=("youtube.upload",))
    log = lab.run_experiments(yt, FakeBrain({}), CH, {}, AT)
    assert yt.calls == [] and "force-ssl" in log[0]


def test_a_hands_off_channel_is_never_touched():
    yt = FakeYT([yt_vid(1, 5, 30)])
    assert lab.run_experiments(yt, FakeBrain({}), dict(CH, hands_off=True), {}, AT) == [] and yt.calls == []


def test_localize_keeps_the_original_description_and_every_snippet_field():
    ch = {"name": "Orbitwire", "about": "", "lab": {"language": "en", "experiments": ["localize"], "languages": ["es", "hi"]}}
    yt = FakeYT([yt_vid(1, 20, 500, title="Quake in 3D #shorts")])
    b = FakeBrain({"Spanish": {"title": "Terremoto en 3D #shorts", "summary": "Párrafo uno."},
                   "Hindi": {"title": "3D में भूकंप", "summary": "पहला"}})
    log = lab.run_experiments(yt, b, ch, {}, AT)
    (_, path, params, body), = [c for c in yt.calls if c[1] == "videos"]
    assert params["part"] == "snippet,localizations" and set(body["localizations"]) == {"es"}
    assert body["localizations"]["es"]["description"].endswith("Credits: CC BY")
    assert body["snippet"]["categoryId"] == "24" and body["snippet"]["tags"] == ["a"] and body["snippet"]["defaultLanguage"] == "en"
    assert "es" in log[0]


def test_a_stalled_upload_is_retitled_once_and_judged_after_three_days(monkeypatch):
    monkeypatch.setattr(lab, "suggestions", lambda q, l: ["jadui ketli kahani"])
    ch = dict(CH, lab={"language": "hi", "experiments": ["retitle"]})
    vids = [yt_vid(1, 60, 20, title="पुरानी केतली #shorts")] + [yt_vid(i, 70 + i, 400) for i in range(2, 9)]
    yt, state = FakeYT(vids), {}
    b = FakeBrain({"far fewer views": {"title": "केतली ने ज़मींदार को क्या दिखाया? #shorts"}})
    log = lab.run_experiments(yt, b, ch, state, AT)
    (_, _, params, body), = [c for c in yt.calls if c[1] == "videos"]
    assert body["snippet"]["title"].startswith("केतली ने") and body["snippet"]["description"].startswith("Para one")
    assert "jadui ketli kahani" in b.prompts[0] and "retitled" in log[0]
    later = AT + timedelta(hours=73)
    vids[0]["statistics"]["viewCount"] = "200"
    log = lab.run_experiments(FakeYT(vids), b, ch, state, later)
    assert state["retitles"][0]["won"] is True and "helped" in log[0]


def test_retitle_switches_itself_off_when_it_does_not_help():
    state = {"retitles": [{"id": f"y{i}", "won": False, "at": ago(200), "after_per_day": 1, "before_per_day": 5,
                           "views_before": 1, "new": "n"} for i in range(6)]}
    log = lab.retitle(FakeYT([]), lambda p: None, CH, [], state, AT, "hi", "", False)
    assert "switched off" in log[-1] and state["retitle_off"]


def test_a_new_title_that_drops_hashtags_or_shouts_is_refused(monkeypatch):
    monkeypatch.setattr(lab, "suggestions", lambda q, l: [])
    vids = [yt_vid(1, 60, 20, title="Old #shorts")] + [yt_vid(i, 70 + i, 400) for i in range(2, 9)]
    for bad in ("New title without tag", "SHOUTING #SHORTS"):
        yt = FakeYT(vids)
        lab.retitle(yt, lambda p, bad=bad: {"title": bad}, CH, vids, {}, AT, "hi", "", False)
        assert yt.calls == []


# ---- tidy ------------------------------------------------------------------------------------------------
class TidyGH:
    def __init__(self, issues, runs, feed_xml):
        self.issues, self.runs, self.feed_xml = issues, runs, feed_xml

    def get(self, path, **params):
        if path.endswith("/issues"):
            return self.issues
        return {"workflow_runs": self.runs.get(path.split("/workflows/")[1].split("/")[0], [])}

    def text(self, url):
        return self.feed_xml


def atom(published):
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns:media="http://search.yahoo.com/mrss/"><entry><yt:videoId>v1</yt:videoId><title>T</title>'
            f'<published>{published}</published></entry></feed>')


LANE = {"workflows": {"run.yml": {"name": "run", "max_h": 2}, "tests.yml": {"name": "tests"}}}


def test_an_alert_is_closed_only_when_runs_are_green_since_and_the_channel_published():
    issue = {"number": 6, "title": "Studio run failed", "updated_at": ago(8)}
    ok = {"run.yml": [{"conclusion": "success", "created_at": ago(1)}]}
    gh = TidyGH([issue], ok, atom(ago(2)))
    (closed, why), = tidy.stale(gh, "o/r", LANE, {"youtube": "UCx"}, AT)
    assert closed["number"] == 6 and "run.yml succeeded since" in why
    assert tidy.stale(TidyGH([issue], ok, atom(ago(20))), "o/r", LANE, {"youtube": "UCx"}, AT) == []
    failing = {"run.yml": [{"conclusion": "failure", "created_at": ago(1)}]}
    assert tidy.stale(TidyGH([issue], failing, atom(ago(2))), "o/r", LANE, {"youtube": "UCx"}, AT) == []
    fresh = dict(issue, updated_at=ago(2))
    assert tidy.stale(TidyGH([fresh], ok, atom(ago(1))), "o/r", LANE, {"youtube": "UCx"}, AT) == []


def test_tidy_leaves_fleet_health_and_owner_alerts_alone():
    ok = {"run.yml": [{"conclusion": "success", "created_at": ago(1)}]}
    for title in ("Channel fleet needs attention", "Kahaani Chaos needs attention"):
        issue = {"number": 8, "title": title, "updated_at": ago(8)}
        assert tidy.stale(TidyGH([issue], ok, atom(ago(2))), "o/r", LANE, {"youtube": "UCx"}, AT) == []


def test_an_upload_gap_alert_closes_once_the_channel_published_after_it():
    issue = {"number": 6, "title": "No upload in the last 30 hours", "updated_at": ago(40)}
    failing = {"run.yml": [{"conclusion": "failure", "created_at": ago(1)}]}
    (closed, why), = tidy.stale(TidyGH([issue], failing, atom(ago(3))), "o/r", LANE, {"youtube": "UCx"}, AT)
    assert closed["number"] == 6 and "published 3 h ago" in why
    assert tidy.stale(TidyGH([issue], failing, atom(ago(50))), "o/r", LANE, {"youtube": "UCx"}, AT) == []


def test_a_silent_channel_gets_its_latest_green_producer_run_healed():
    lane = {"mode": "fix", "workflows": {"run.yml": {"name": "run", "max_h": 2}, "health.yml": {"name": "health", "max_h": 9}}}
    runs = {"run.yml": [{"id": 9, "conclusion": "failure"}, {"id": 8, "conclusion": "success"}],
            "health.yml": [{"id": 7, "conclusion": "success"}]}
    ch = {"name": "C", "youtube": "UCx", "quiet_h": 10, "min_24h": 2}
    run, why = tidy.silent_run(TidyGH([], runs, atom(ago(30))), "o/r", lane, ch, AT)
    assert run["id"] == 8 and "no new public upload for 30 h" in why
    run, why = tidy.silent_run(TidyGH([], runs, atom(ago(3))), "o/r", lane, ch, AT)
    assert run["id"] == 8 and "only 1 public uploads" in why
    assert tidy.silent_run(TidyGH([], runs, atom(ago(3))), "o/r", lane, dict(ch, min_24h=1), AT) is None
    for other in ({"hands_off": True}, {"drill": True}):
        assert tidy.silent_run(TidyGH([], runs, atom(ago(30))), "o/r", lane, dict(ch, **other), AT) is None
    assert tidy.silent_run(TidyGH([], runs, atom(ago(30))), "o/r", dict(lane, mode="watch"), ch, AT) is None


# ---- the local Qwen brain and the alternatives ----------------------------------------------------------
def test_the_brain_asks_local_qwen_only_when_gemini_gives_nothing(monkeypatch):
    monkeypatch.setenv("DOCTOR_LOCAL", "1")
    asked = []
    monkeypatch.setattr(local, "ask_json", lambda p: asked.append(p) or {"diagnosis": "x"})
    b = brain_mod.Brain(key="")
    assert b.ready and b.ask_json("why?") == {"diagnosis": "x"} and b.used == local.NAME
    monkeypatch.setattr(brain_mod.Brain, "_gemini", lambda self, *a: {"ok": 1})
    asked.clear()
    assert brain_mod.Brain(key="k").ask_json("why?") == {"ok": 1} and asked == []


def test_a_failed_local_model_is_not_retried_in_the_same_job(monkeypatch):
    monkeypatch.setenv("DOCTOR_LOCAL", "1")
    calls = []
    monkeypatch.setattr(local, "ask_json", lambda p: calls.append(1) or (_ for _ in ()).throw(OSError("disk")))
    b = brain_mod.Brain(key="")
    assert b.ask_json("a") is None and b.down and b.ask_json("b") is None and len(calls) == 1


def test_one_local_server_serves_the_whole_job_within_its_budget(monkeypatch):
    started, posted = [], []

    class Proc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, t):
            pass

    class Resp:
        status = 200

        def __init__(self, data=b"{}"):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, *a):
            return self.data

    def urlopen(req, timeout=None):
        if isinstance(req, str):
            return Resp()
        posted.append(timeout)
        return Resp(b'{"choices": [{"message": {"content": "{\\"ok\\": 1}"}}]}')

    monkeypatch.setattr(local, "server", lambda: local.DIR / "llama-server")
    monkeypatch.setattr(local, "weights", lambda: local.DIR / "m.gguf")
    monkeypatch.setattr(local.subprocess, "Popen", lambda *a, **k: started.append(1) or Proc())
    monkeypatch.setattr(local.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(local._Server, "proc", None)
    monkeypatch.setattr(local._Server, "started", None)
    assert local.ask_json("a") == {"ok": 1} and local.ask_json("b") == {"ok": 1} and len(started) == 1
    assert max(posted) <= local.BUDGET_S
    monkeypatch.setattr(local._Server, "started", local.time.monotonic() - local.BUDGET_S)
    import pytest
    with pytest.raises(RuntimeError, match="budget"):
        local.ask_json("c")


def test_without_the_switch_there_is_no_local_brain(monkeypatch):
    monkeypatch.delenv("DOCTOR_LOCAL", raising=False)
    assert not brain_mod.Brain(key="").ready


def test_a_long_prompt_keeps_its_instructions_and_its_latest_log():
    text = "RULES " + "x" * 50_000 + " LAST-LINE"
    cut = local.fit(text, 1000)
    assert len(cut) < 1100 and cut.startswith("RULES") and cut.endswith("LAST-LINE")


def test_alternatives_give_the_general_rules_and_one_channels_section():
    one = mission.alternatives("kahaani-chaos")
    assert "### general" in one and "### kahaani-chaos" in one and "### orbitwire" not in one and "Charon" in one
    every = mission.alternatives()
    assert "### orbitwire" in every and "### universe" in every
