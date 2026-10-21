import urllib.error
from datetime import datetime, timedelta, timezone

from doctor import growth, review

AT = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)


def _run(h, conclusion="success"):
    return {"created_at": (AT - timedelta(hours=h)).isoformat(), "status": "completed", "conclusion": conclusion}


class PagedGH:
    """Workflow runs served 100 a page, newest first, like the Actions API."""

    def __init__(self, runs):
        self.runs, self.calls = runs, []

    def get(self, path, **params):
        self.calls.append((path, params))
        wf = path.split("/workflows/")[1].split("/")[0]
        if wf not in self.runs:
            raise urllib.error.HTTPError(path, 404, "nf", {}, None)
        page = params.get("page", 1)
        return {"workflow_runs": self.runs[wf][(page - 1) * 100:page * 100]}


def test_reliability_reads_the_whole_week_and_names_silent_workflows():
    every_15_min = [_run(i / 4, "failure" if i in (5, 250) else "success") for i in range(280)]
    gh = PagedGH({"run.yml": every_15_min, "studio.yml": []})
    ch = {"lanes": [{"repo": "r", "workflows": {"run.yml": {"name": "run"}, "studio.yml": {"name": "studio"},
                                                 "watchdog.yml": {"name": "watchdog"}}}]}
    line = review.reliability(gh, "o", ch, AT)
    # the second failure is on page 3: one page (the old read) would have said 1 of 100
    assert "run: 2 of 280 runs failed" in line and "only the latest" not in line
    assert "studio: no runs in the last 7 days" in line and "watchdog: run history unreadable" in line
    assert all(p["created"] == ">=2026-10-15" for _, p in gh.calls)


def test_reliability_says_when_it_could_not_reach_back_a_week():
    gh = PagedGH({"run.yml": [_run(i / 10) for i in range(1000)]})
    line = review.reliability(gh, "o", {"lanes": [{"repo": "r", "workflows": {"run.yml": {"name": "run"}}}]}, AT)
    assert line == f"run: 0 of {100 * review.RUN_PAGES} runs failed (only the latest runs, back 80 h)"


def test_feed_unknown_forbids_inferring_uploads_from_green_runs():
    assert "green workflow runs are not evidence of uploads" in review.FEED_UNKNOWN
    assert "zero" in review.FEED_UNKNOWN and "unknown" in review.FEED_UNKNOWN
    assert "the About text is the plan, not evidence" in review.PROMPT


def test_title_flags():
    assert review.title_flags("Greece Spends 165.1% Of Its GDP") == ["percentage over 100% (165.1%)"]
    assert review.title_flags("Niger has 0.0 doctors per 1,000 people") == ["zero value '0.0'"]
    assert review.title_flags("places where nobody owns a car") == ["starts in lower case"]
    assert review.title_flags("Japan Has 30% Fewer Cars Than 2000 #shorts") == []
    assert review.title_flags("Norway Has 10,000 Fjords And 0.05% Of Them Are Named") == []
    assert review.title_flags("क्यों बढ़े सोने के दाम? 100% सच") == []


def test_lint_merges_feed_and_api_titles_and_reports_pipeline_tags():
    vids = [{"id": "a", "title": "places where it never rains"}]
    analytics = {"recent": [["a", "places where it never rains", "2026-10-21T12:00:00Z", []],
                            ["b", "Chile Has 0.0 Rain", "2026-10-21T08:00:00Z", ["rebuilt-in-3d:wk-Q42"]],
                            ["c", "Fine Title", "2026-10-20T08:00:00Z", []]],
                 "keywords_machine": ["hindi-news-desk:kh-1f2e"]}
    flags = review.lint(vids, analytics)
    assert flags[:2] == ['a "places where it never rains": starts in lower case', 'b "Chile Has 0.0 Rain": zero value \'0.0\'']
    assert "1 of the latest 3 public uploads carry pipeline tags" in flags[2] and "rebuilt-in-3d:wk-Q42" in flags[2]
    assert flags[3] == "channel keywords carry pipeline tags: hindi-news-desk:kh-1f2e"
    assert review.lint([], None) == []


def test_api_uploads_stand_in_for_an_unreadable_feed_only_while_fresh():
    analytics = {"at": (AT - timedelta(hours=20)).isoformat(),
                 "recent": [["b", "Two", "2026-10-21T08:00:00Z", []], ["a", "One", "2026-10-11T08:00:00Z", []]]}
    vids = review.api_uploads(analytics, AT)
    assert [v["id"] for v in vids] == ["b", "a"] and all(v["views"] is None for v in vids)
    table, summary = review.uploads_table(vids, AT)
    assert "Two" in table and summary.startswith("1 uploads in the last 7 days")
    assert review.api_uploads(dict(analytics, at=(AT - timedelta(hours=50)).isoformat()), AT) == []
    assert review.api_uploads({}, AT) == []


def test_prompt_takes_every_field():
    keys = ("mission", "name", "about", "road", "uploads", "summary", "experiments", "reliability", "learnings", "rules",
            "hands_off", "lint")
    assert "Title and tag lint" in review.PROMPT.format(**{k: "x" for k in keys})


def test_uploads_meta_keeps_public_uploads_and_pipeline_tags(monkeypatch):
    seen = []

    def fake(url, data=None, token=None):
        seen.append(url)
        if "playlistItems" in url:
            assert "playlistId=UUx" in url
            return {"items": [{"contentDetails": {"videoId": v}} for v in ("v1", "v2", "v3")]}
        return {"items": [
            {"id": "v1", "snippet": {"title": "Old", "publishedAt": "2026-10-20T00:00:00Z", "tags": ["quakes"]},
             "status": {"privacyStatus": "public"}},
            {"id": "v2", "snippet": {"title": "Scheduled", "publishedAt": "2026-10-22T00:00:00Z"},
             "status": {"privacyStatus": "private"}},
            {"id": "v3", "snippet": {"title": "New", "publishedAt": "2026-10-21T00:00:00Z",
                                     "tags": ["rebuilt-in-3d:wk-Q42", "earthquake", "a:b c"]},
             "status": {"privacyStatus": "public"}}]}

    monkeypatch.setattr(growth, "_json", fake)
    ch = {"contentDetails": {"relatedPlaylists": {"uploads": "UUx"}},
          "brandingSettings": {"channel": {"keywords": 'history "dark history" hindi-news-desk:kh-1f2e'}}}
    out = growth.uploads_meta(ch, "t")
    assert out["recent"] == [["v3", "New", "2026-10-21T00:00:00Z", ["rebuilt-in-3d:wk-Q42"]],
                             ["v1", "Old", "2026-10-20T00:00:00Z", []]]
    assert out["keywords_machine"] == ["hindi-news-desk:kh-1f2e"] and len(seen) == 2
    assert growth.keywords('a "b c') == ["a", '"b', "c"]


def test_analytics_reads_uploads_and_a_failed_listing_never_costs_the_numbers(monkeypatch):
    def fake(url, data=None, token=None):
        if "oauth2" in url:
            return {"access_token": "t", "scope": "https://www.googleapis.com/auth/youtube.force-ssl "
                                                  "https://www.googleapis.com/auth/yt-analytics.readonly"}
        if "/channels?" in url:
            return {"items": [{"id": "UCx", "statistics": {"subscriberCount": "30", "viewCount": "15000", "videoCount": "40"},
                               "contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}, "brandingSettings": {}}]}
        if "playlistItems" in url:
            if fake.broken:
                raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
            return {"items": [{"contentDetails": {"videoId": "v1"}}]}
        if "/videos?" in url:
            return {"items": [{"id": "v1", "snippet": {"title": "T", "publishedAt": "2026-10-21T00:00:00Z"},
                               "status": {"privacyStatus": "public"}}]}
        if "dimensions=video" in url:
            return {"rows": []}
        if "creatorContentType" in url:
            return {"rows": [["SHORTS", 1000, 800, 900], ["VIDEO_ON_DEMAND", 10, 10, 120]]}
        return {"rows": [[1000, 5, 1]]}

    fake.broken = False
    monkeypatch.setattr(growth, "_json", fake)
    cred = '{"client_id": "c", "client_secret": "s", "refresh_token": "r"}'
    out = growth.analytics(cred, today=AT.date())
    assert out["recent"] == [["v1", "T", "2026-10-21T00:00:00Z", []]] and out["keywords_machine"] == []
    assert out["long_hours_365d"] == 2.0 and out["shorts_views_90d"] == 800 and out["write_scope"] is True
    fake.broken = True
    out = growth.analytics(cred, today=AT.date())
    assert out["uploads_error"] == "403" and "recent" not in out and out["long_hours_365d"] == 2.0


def test_title_flags_spare_changes_hindi_doubling_and_read_thousands():
    assert review.title_flags("China's Coal Use Rose 340% Since 2000") == []
    assert review.title_flags("Solar Is 1,200% Bigger Than In 2010") == []
    assert review.title_flags("Japan Spends 1,000% Of Its Budget") == ["percentage over 100% (1,000%)"]
    assert review.title_flags("Japan Has 0,5% Of It") == []
    assert review.title_flags("धीरे धीरे बदल गया भारत", "hi") == []
    assert review.title_flags("Kya kya hua aaj", "hi") == []
    assert review.title_flags("The the Last Train") == ["repeated word 'the'"]


class FlakyGH(PagedGH):
    def get(self, path, **params):
        if params.get("page", 1) > 1:
            raise OSError("reset")
        return super().get(path, **params)


def test_reliability_keeps_pages_read_before_an_error():
    gh = FlakyGH({"run.yml": [_run(i / 10, "failure" if i == 3 else "success") for i in range(300)]})
    line = review.reliability(gh, "o", {"lanes": [{"repo": "r", "workflows": {"run.yml": {"name": "run"}}}]}, AT)
    assert line == "run: 1 of 100 runs failed (only the latest runs, back 10 h; later pages unreadable)"
