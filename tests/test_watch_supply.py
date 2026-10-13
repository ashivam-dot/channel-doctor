from datetime import datetime, timedelta, timezone

from doctor import review, watch
from tests.test_doctor import FakeGH


def _atom(*published):
    entries = "".join(
        f"<entry><yt:videoId>v{i}</yt:videoId><title>T{i}</title><published>{p}</published><media:group>"
        f"<media:community><media:statistics views=\"5\"/></media:community></media:group></entry>"
        for i, p in enumerate(published))
    return (f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
            f'xmlns:media="http://search.yahoo.com/mrss/">{entries}</feed>')


AT = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)


def _ago(h):
    return (AT - timedelta(hours=h)).isoformat()


def test_a_green_lane_that_publishes_too_little_is_flagged_thin():
    cfg = {"owner": "o", "channels": {
        "a": {"name": "A", "youtube": "UCa", "quiet_h": 10, "min_24h": 3, "lanes": []},
        "b": {"name": "B", "youtube": "UCb", "quiet_h": 10, "min_24h": 3, "lanes": []}}}
    gh = FakeGH({}, {"UCa": _atom(_ago(2), _ago(30)), "UCb": _atom(_ago(1), _ago(5), _ago(9))})
    problems, _ = watch.check(gh, cfg, AT)
    assert set(problems) == {"thin:a"}


def test_feed_falls_back_to_the_uploads_playlist(monkeypatch):
    monkeypatch.setattr(watch, "FEED_PAUSES_S", (0, 0))
    gh = FakeGH({}, {"UUa": _atom(_ago(1))})
    assert [v["id"] for v in watch.feed(gh, "UCa")] == ["v0"]


def test_an_unreadable_feed_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(watch, "FEED_PAUSES_S", (0, 0))
    cfg = {"owner": "o", "channels": {"a": {"name": "A", "youtube": "UCa", "quiet_h": 10, "lanes": []}}}
    problems, status = watch.check(FakeGH({}, {}), cfg, AT)
    assert problems == {"blind:feeds": "YouTube feeds answered for no channel: the upload watch is blind"}
    assert "zero" in review.FEED_UNKNOWN and "unknown" in review.FEED_UNKNOWN
