from datetime import datetime, timedelta, timezone

from doctor import telegram, watch
from tests.test_doctor import FakeGH

AT = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)
CFG = {"owner": "o", "channels": {
    "atlas": {"name": "Atlas in Numbers", "youtube": "UCa", "lanes": []},
    "kahaani-chaos": {"name": "Kahaani Chaos", "youtube": "UCk", "lanes": []},
    "drill": {"name": "Drill", "youtube": "UCd", "drill": True, "lanes": []}}}


def _vid(i, h, views):
    return {"id": f"v{i}", "title": f"Video {i}", "published": (AT - timedelta(hours=h)).isoformat(), "views": views}


def _ctx():
    return {"at": AT, "road": None,
            "status": {"Atlas in Numbers": ["last upload 2.0 h ago"], "Kahaani Chaos": ["no public upload yet"]},
            "problems": {"quiet:kahaani-chaos": "Kahaani Chaos: no public upload at all"},
            "videos": {"Atlas in Numbers": [_vid(1, 2, 120), _vid(2, 30, 900), _vid(3, 24 * 9, 5000)],
                       "Kahaani Chaos": []}}


def test_a_message_parses_into_command_and_rest_and_plain_text_is_a_question():
    assert telegram.parse("/channel@AkshChannelDoctorBot  Kahaani chaos ") == ("/channel", "Kahaani chaos")
    assert telegram.parse("/STATUS") == ("/status", "")
    assert telegram.parse("why is atlas slow?") == ("/ask", "why is atlas slow?")


def test_only_the_owner_chat_is_heard(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5")
    res = {"result": [{"update_id": 9, "message": {"chat": {"id": 5}, "text": "/top 3"}},
                      {"update_id": 10, "message": {"chat": {"id": 6}, "text": "/status"}},
                      {"update_id": 11, "message": {"chat": {"id": 5}, "text": "hello there"}}]}
    seen = {}
    monkeypatch.setattr(telegram, "_api", lambda method, wait=40, **p: seen.update(p, wait=wait) or res)
    msgs, offset = telegram.messages(0, wait=30)
    assert msgs == [("/top", "3"), ("/ask", "hello there")] and offset == 12
    assert seen["timeout"] == 30 and seen["wait"] > 30


def test_a_channel_is_found_by_any_part_of_its_name_but_never_a_drill():
    assert watch.find_channel("kahaani", CFG)["name"] == "Kahaani Chaos"
    assert watch.find_channel("atlas in numbers", CFG)["name"] == "Atlas in Numbers"
    assert watch.find_channel("numbers", CFG)["name"] == "Atlas in Numbers"
    assert watch.find_channel("drill", CFG) is None and watch.find_channel("", CFG) is None


def test_channel_shows_state_uploads_and_its_own_problems():
    text = watch.respond("/channel", "kahaani", _ctx(), None, CFG)
    assert text.startswith("Kahaani Chaos") and "no public upload at all" in text
    atlas = watch.respond("/channel", "atlas", _ctx(), None, CFG)
    assert "https://youtu.be/v1" in atlas and "Problems: none" in atlas
    assert "Channels: Atlas in Numbers, Kahaani Chaos" in watch.respond("/channel", "", _ctx(), None, CFG)


def test_top_ranks_the_window_by_views():
    text = watch.respond("/top", "", _ctx(), None, CFG)
    assert text.index("900 views") < text.index("120 views") and "5,000" not in text
    assert "5,000 views" in watch.respond("/top", "10", _ctx(), None, CFG)


def test_a_question_is_answered_from_the_fleet_facts():
    class Brain:
        ready, used = True, "gemini-x"

        def ask_json(self, prompt, *a, **k):
            self.prompt = prompt
            return {"answer": "Kahaani Chaos has not published yet."}

    b = Brain()
    text = watch.ask("why is kahaani quiet?", _ctx(), b)
    assert text.startswith("Kahaani Chaos has not published yet.") and "gemini-x" in text
    assert "why is kahaani quiet?" in b.prompt and "no public upload at all" in b.prompt and "Video 2" in b.prompt


def test_a_question_without_a_key_or_an_answer_says_so():
    class Brain:
        ready, used = False, None

    assert "DOCTOR_GEMINI_API_KEY" in watch.ask("hi", _ctx(), Brain())
    Brain.ready = True
    Brain.ask_json = lambda self, *a, **k: None
    assert "try again" in watch.ask("hi", _ctx(), Brain())


def test_id_and_unknown_commands(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "77")
    assert watch.respond("/id", "", _ctx(), None, CFG).endswith("77")
    assert watch.respond("/nope", "", _ctx(), None, CFG) == watch.HELP


def test_review_is_dispatched_unless_dry():
    posted = []
    gh = FakeGH({}, {})
    gh.post = lambda path, body: posted.append(path)
    assert "Started the review" in watch.respond("/review", "", _ctx(), gh, CFG, dry=True) and not posted
    watch.respond("/scout", "", _ctx(), gh, CFG)
    assert posted == ["/repos/" + watch.HOME + "/actions/workflows/scout.yml/dispatches"]


def test_listening_replies_as_messages_arrive_and_saves_the_offset(monkeypatch):
    clock, batches = [0], iter([([("/id", "")], 4), ([], 4)])
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock[0])

    def poll(offset, wait=0):
        clock[0] += 100
        return next(batches)

    monkeypatch.setattr(telegram, "messages", poll)
    sent = []
    monkeypatch.setattr(telegram, "send", sent.append)

    class St:
        data, saves = {"tg_offset": 3}, 0

        def save(self):
            St.saves += 1

    watch.listen(St(), _ctx(), None, CFG, until=150)
    assert len(sent) == 1 and St.data["tg_offset"] == 4 and St.saves == 1
