"""Road to YPP: where each channel stands against the monetisation finish lines, and how fast it is moving.

Public numbers (subscribers, listed videos' views) come from the channel pages and feed, for every channel.
Exact numbers (Shorts views in 90 days, long-form watch hours in 365 days) come from YouTube Analytics, read inside a
channel's own repo with its own credential (`growth` mode), and are left in that repo's Doctor log state for the
central watch to pick up."""
from __future__ import annotations

import json
import os
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

FINISH = {
    "expanded": {"label": "500-sub tier (fan funding)", "subs": 500, "hours": 3000, "shorts": 3_000_000},
    "full": {"label": "full YPP (ad revenue)", "subs": 1000, "hours": 4000, "shorts": 10_000_000},
}
DEADLINE = date(2027, 1, 31)
# Dates the watch-hour and qualified-view totals have to be met by. A YPP review takes about a month and a rejection
# costs 30 days, so applying under today's bar means being over the line by mid-December; from 1 Feb 2027 new
# applicants need double.
WINDOWS = ({"by": date(2026, 12, 15), "hours": 4000, "views": 10_000_000, "label": "by 15 Dec"},
           {"by": date(2027, 2, 1), "hours": 8000, "views": 20_000_000, "label": "by 1 Feb (doubled bar)"})
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 Chrome/128 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}


def count(text: str | None) -> int | None:
    if not text:
        return None
    m = re.match(r"([\d.,]+)\s*([KMB]?)", text.strip().replace("\u202f", ""))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def _page(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


ITEM = re.compile(r'"(?:videoId|contentId)":\s*"([\w-]{11})".{0,4000}?"(?:accessibilityText|simpleText|content|label)":\s*"[^"]*?([\d.,]+[KMB]?) views', re.S)


def listed(html: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for vid, views in ITEM.findall(html):
        out.setdefault(vid, count(views) or 0)
    return out


def public(channel_id: str) -> dict:
    """Subscribers and the views of the videos the channel page lists (about the latest 30-50 of each kind)."""
    base = f"https://www.youtube.com/channel/{channel_id}"
    home = _page(base)
    subs = re.search(r"([\d.,]+[KMB]?) subscribers?\b", home)
    shorts = listed(_page(base + "/shorts"))
    longs = {k: v for k, v in listed(_page(base + "/videos")).items() if k not in shorts}
    return {"subs": count(subs.group(1)) if subs else None, "shorts_listed": len(shorts),
            "shorts_views_listed": sum(shorts.values()), "long_listed": len(longs), "long_views_listed": sum(longs.values())}


# ---- exact numbers, run inside the channel's own repo --------------------------------------------------------
def _json(url: str, data: dict | None = None, token: str | None = None):
    body = urllib.parse.urlencode(data).encode() if data else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=60) as r:
        return json.load(r)


def analytics(raw_credential: str, today: date | None = None) -> dict:
    info = json.loads(raw_credential)
    try:
        grant = _json(info.get("token_uri") or "https://oauth2.googleapis.com/token",
                      {"client_id": info["client_id"], "client_secret": info["client_secret"],
                       "refresh_token": info["refresh_token"], "grant_type": "refresh_token"})
    except urllib.error.HTTPError as e:
        return {"error": f"token refresh HTTP {e.code}"}
    token = grant["access_token"]
    scopes = set(grant.get("scope", "").split())
    out: dict = {"analytics_scope": "yt-analytics" in grant.get("scope", ""),
                 # videos.update (retitle, untag) needs one of these; youtube.upload alone can't edit
                 "write_scope": bool(scopes & {"https://www.googleapis.com/auth/youtube",
                                               "https://www.googleapis.com/auth/youtube.force-ssl"})}
    try:
        ch = _json("https://www.googleapis.com/youtube/v3/channels?part=statistics,snippet,contentDetails,brandingSettings"
                   "&mine=true", token=token)["items"][0]
        st = ch["statistics"]
        out |= {"channel": ch["id"], "subs": int(st.get("subscriberCount", 0)), "views_total": int(st.get("viewCount", 0)),
                "videos": int(st.get("videoCount", 0))}
    except (urllib.error.HTTPError, KeyError, IndexError) as e:
        out["error"] = f"channel stats failed ({getattr(e, 'code', type(e).__name__)})"
        return out
    try:
        out |= uploads_meta(ch, token)
    except (OSError, KeyError, ValueError, TypeError) as e:  # lint input only: never costs the numbers below
        out["uploads_error"] = f"{getattr(e, 'code', type(e).__name__)}"
    end = (today or datetime.now(timezone.utc).date()) - timedelta(days=1)

    def report(days: int, metrics: str, dims: str | None = None):
        q = {"ids": "channel==MINE", "startDate": str(end - timedelta(days=days - 1)), "endDate": str(end), "metrics": metrics}
        if dims:
            q["dimensions"] = dims
        return _json("https://youtubeanalytics.googleapis.com/v2/reports?" + urllib.parse.urlencode(q), token=token).get("rows") or []

    try:
        for days, key in ((90, "90d"), (365, "365d")):
            # YPP counts qualified (engaged) Shorts views; plain `views` counts every play start since 2026-08-24.
            try:
                rows = report(days, "views,engagedViews,estimatedMinutesWatched", "creatorContentType")
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                rows = [[k, v, v, m] for k, v, m in report(days, "views,estimatedMinutesWatched", "creatorContentType")]
            for kind, views, engaged, minutes in rows:
                kind = str(kind).upper()
                if kind == "SHORTS":
                    out[f"shorts_views_{key}"] = out.get(f"shorts_views_{key}", 0) + int(engaged)
                    out[f"shorts_plays_{key}"] = out.get(f"shorts_plays_{key}", 0) + int(views)
                elif kind in ("VIDEO_ON_DEMAND", "VIDEOONDEMAND", "LIVE_STREAM", "LIVESTREAM"):
                    out[f"long_hours_{key}"] = round(out.get(f"long_hours_{key}", 0) + minutes / 60, 1)
        rows = report(28, "views,subscribersGained,subscribersLost")
        if rows:
            out |= {"views_28d": int(rows[0][0]), "subs_net_28d": int(rows[0][1]) - int(rows[0][2])}
    except urllib.error.HTTPError as e:
        out["analytics_error"] = f"HTTP {e.code}" + ("" if out["analytics_scope"] else
                                                      ": the sign-in lacks the yt-analytics.readonly scope")
        return out
    out.setdefault("shorts_views_90d", 0)
    out.setdefault("long_hours_365d", 0.0)
    out["through"] = str(end)
    # Per upload, 28 days: [video, views, engaged views, average % viewed, average seconds, subscribers gained].
    # Engaged views / views is the closest API proxy for Studio's "viewed vs swiped away".
    q = {"ids": "channel==MINE", "startDate": str(end - timedelta(days=27)), "endDate": str(end),
         "metrics": "views,engagedViews,averageViewPercentage,averageViewDuration,subscribersGained",
         "dimensions": "video", "sort": "-views", "maxResults": 25}
    try:
        per = _json("https://youtubeanalytics.googleapis.com/v2/reports?" + urllib.parse.urlencode(q), token=token)
        out["per_video"] = [[v, int(n), int(e), round(float(p), 1), round(float(d)), int(s)]
                            for v, n, e, p, d, s in per.get("rows") or []]
    except (urllib.error.HTTPError, ValueError) as e:
        out["per_video_error"] = f"{getattr(e, 'code', type(e).__name__)}"
    return out


# A pipeline's own bookkeeping tag (rebuilt-in-3d:wk-Q42, hindi-news-desk:kh-1f2e): meaningless to viewers and search.
MACHINE_TAG = re.compile(r"^[a-z0-9][a-z0-9-]*:[\w-]+$")
RECENT = 15


def keywords(text: str) -> list[str]:
    """brandingSettings.channel.keywords: space separated, multi-word keywords in double quotes."""
    try:
        return shlex.split(text or "")
    except ValueError:
        return (text or "").split()


def uploads_meta(ch: dict, token: str) -> dict:
    """The latest public uploads as the owner's sign-in sees them (tags included), for the review's title lint and for
    when the public feed is unreadable: [video, title, published, machine tags]. Private and scheduled ones are left
    out: this lands in the repo's state issue. Plus machine tags in the channel keywords. 2 quota units."""
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    items = _json("https://www.googleapis.com/youtube/v3/playlistItems?" + urllib.parse.urlencode(
        {"part": "contentDetails", "playlistId": uploads, "maxResults": RECENT}), token=token).get("items") or []
    ids = [i["contentDetails"]["videoId"] for i in items]
    recent = []
    if ids:
        vids = _json("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
            {"part": "snippet,status", "id": ",".join(ids)}), token=token).get("items") or []
        for v in vids:
            sn = v.get("snippet") or {}
            if (v.get("status") or {}).get("privacyStatus") == "public":
                recent.append([v["id"], sn.get("title", ""), sn.get("publishedAt"),
                               [t for t in sn.get("tags") or [] if MACHINE_TAG.match(t)][:3]])
    recent.sort(key=lambda r: r[2] or "", reverse=True)
    words = keywords(((ch.get("brandingSettings") or {}).get("channel") or {}).get("keywords", ""))
    return {"recent": recent, "keywords_machine": [w for w in words if MACHINE_TAG.match(w)]}


GROWTH_TITLE = "Doctor growth"
GROWTH_INTRO = ("Channel Doctor's daily YouTube Analytics numbers for this channel (subscribers, Shorts views in 90 days, "
                "long-form watch hours in 365 days), read by the central Road-to-YPP digest.")


def run_repo(repo: str, dry: bool = False):
    """`growth` mode: exact numbers for this repo's channel, left in its own state issue for the central watch."""
    from .github import GitHub
    from .state import IssueState, iso

    raw = os.environ.get("YOUTUBE_OAUTH_JSON")
    if not raw:
        print("no YOUTUBE_OAUTH_JSON in this job; growth numbers skipped")
        return
    res = analytics(raw)
    res["at"] = iso()
    print("growth:", json.dumps({k: v for k, v in res.items() if k not in ("channel", "recent")}))
    st = IssueState(GitHub(), repo, GROWTH_TITLE, "doctor", GROWTH_INTRO, dry)
    st.data = res
    st.save()


def exact_for(gh, owner: str, ch: dict) -> dict | None:
    from .state import IssueState

    for lane in ch.get("lanes", []):
        if lane.get("analytics"):
            try:
                data = IssueState(gh, f"{owner}/{lane['repo']}", GROWTH_TITLE, "doctor", "", dry=True).data
            except Exception:
                continue
            if data:
                return data
    return None


MILESTONES = (100, 250, 500, 1000)


def snapshot(gh, cfg: dict, state: dict, now: datetime) -> tuple[list[str], dict, list[str]]:
    """Daily Road-to-YPP pass. Updates state["growth"] (history per channel) and state["milestones"].
    Returns (digest lines, per-channel info for reviews, milestone announcements)."""
    history: dict = state.setdefault("growth", {})
    seen: dict = state.setdefault("milestones", {})
    today = now.date()
    lines, info, news = [], {}, []
    for key, ch in cfg["channels"].items():
        if ch.get("drill") or not ch.get("youtube"):
            continue
        try:
            pub = public(ch["youtube"])
        except Exception as exc:
            print(f"{ch['name']}: channel page unreadable ({type(exc).__name__})")
            pub = {}
        exact = exact_for(gh, cfg["owner"], ch)
        m = merged(pub, exact, now)
        if m["subs"] is not None:
            rows = [r for r in history.get(key, []) if r[0] != str(today)]
            rows.append([str(today), m["subs"], m.get("shorts_views_90d") if m["exact"] else None,
                         m.get("long_hours_365d") if m["exact"] else None])
            history[key] = rows[-120:]
        route = ch.get("goal", {}).get("route", "shorts")
        line, inf = road(ch["name"], m, history.get(key, []), today, route)
        if inf.get("projection"):
            print(f"projection {ch['name']}: " + "; ".join(inf["projection"]))
        if exact and exact.get("analytics_error"):
            line += f" | analytics: {exact['analytics_error']}"
        lines.append(line)
        info[key] = inf | {"exact": m["exact"], "public": pub, "analytics": {k: v for k, v in (exact or {}).items() if k != "channel"}}
        got = seen.setdefault(key, [])
        for n in MILESTONES:
            if m["subs"] is not None and m["subs"] >= n and str(n) not in got:
                got.append(str(n))
                if history.get(key) and len(history[key]) > 1:
                    news.append(f"{ch['name']} passed {n} subscribers.")
        if m["exact"]:
            for tier, f in FINISH.items():
                ok = m["subs"] >= f["subs"] and ((m.get("shorts_views_90d") or 0) >= f["shorts"] or
                                                 (m.get("long_hours_365d") or 0) >= f["hours"])
                if ok and tier not in got:
                    got.append(tier)
                    news.append(f"{ch['name']} meets the {f['label']} thresholds. Your steps: turn on 2-Step "
                                "Verification, open YouTube Studio > Earn, link AdSense and press Apply.")
    return lines, info, news


# ---- progress -------------------------------------------------------------------------------------------------
def rate(history: list[list], index: int, days: float = 14) -> float | None:
    """Per-day change of history column `index` over the last `days` (history rows: [iso date, subs, shorts90, hours365])."""
    pts = [(date.fromisoformat(r[0]), r[index]) for r in history if r[index] is not None]
    if len(pts) < 2:
        return None
    last = pts[-1]
    first = next((p for p in pts if (last[0] - p[0]).days <= days), pts[0])
    span = (last[0] - first[0]).days
    return (last[1] - first[1]) / span if span >= 2 else None


def eta_days(now_value: float, target: float, per_day: float | None) -> float | None:
    if now_value >= target:
        return 0
    if not per_day or per_day <= 0:
        return None
    return (target - now_value) / per_day


def road(name: str, m: dict, history: list[list], today: date, route: str) -> tuple[str, dict]:
    """One line for Telegram and a dict for the review."""
    subs = m.get("subs")
    if subs is None:
        return f"{name}: no subscriber number yet", {}
    s90, h365 = m.get("shorts_views_90d"), m.get("long_hours_365d")
    sub_rate = rate(history, 1)
    if sub_rate is None and m.get("subs_net_28d"):
        sub_rate = m["subs_net_28d"] / 28
    parts, info = [f"{name}: {subs:,} subs"], {"subs": subs, "subs_per_day": sub_rate, "route": route}
    if route in ("shorts", "both") and s90 is not None:
        if not m.get("exact"):
            parts.append(f"views on listed Shorts {s90:,}")
        else:
            lag = m.get("listed", 0) > s90
            parts.append(f"Shorts views 90d {s90:,}" + (f" (Analytics lags to {m.get('through')}; listed Shorts show "
                                                        f"{m['listed']:,})" if lag else ""))
    if route in ("longform", "both") and h365 is not None:
        parts.append(f"watch hours {h365:,.0f}")
    if route in ("longform", "both") or m.get("exact"):
        info["projection"] = projection(m, history, today, route)
        parts += info["projection"]
    days_left = (DEADLINE - today).days
    for tier, f in FINISH.items():
        sub_eta = eta_days(subs, f["subs"], sub_rate)
        info[f"{tier}_sub_eta_days"] = sub_eta
        pct = min(100, 100 * subs / f["subs"])
        when = "reached" if sub_eta == 0 else ("pace unknown" if sub_eta is None else
                                              f"~{sub_eta:.0f} days at current pace{'' if sub_eta <= days_left else ' (after the deadline)'}")
        parts.append(f"{f['label']}: subs {pct:.0f}%, {when}")
    info["days_to_deadline"] = days_left
    return " | ".join(parts), info


def needed_per_day(have: float, target: float, days_left: int, window: int | None = None) -> float | None:
    """Average a day from today on that reaches `target` on the date `days_left` away. For a rolling `window`-day
    total (Shorts views in 90 d) today's total is taken as evenly spread, and the part that is older than the window
    on that date drops out. Watch hours have no window here: these channels are younger than 365 days."""
    if days_left <= 0:
        return None
    kept = have if window is None else have * max(0, window - days_left) / window
    return max(0.0, (target - kept) / days_left)


def verdict(have: float, target: float, need: float | None, pace: float | None) -> str:
    if have >= target:
        return "reached"
    if need is None:
        return "date passed"
    if pace is None:
        return "pace unknown"
    if pace >= need:
        return "GO (on pace while it holds)"
    return f"NO-GO (pace {100 * pace / need:.1f}% of the need)" if need else "NO-GO"


def projection(m: dict, history: list[list], today: date, route: str) -> list[str]:
    """Go/no-go against each window: what each total needs a day from now on against what it does now. It reports the
    gap; it is not a forecast."""
    if not m.get("exact"):
        return ["watch hours and qualified views unknown (no fresh Analytics numbers for this channel)"]

    def num(x: float | None, places: int) -> str:
        return "?" if x is None else f"{x:,.{places}f}"

    out = []
    if route in ("longform", "both"):
        have = m.get("long_hours_365d") or 0.0
        pace = rate(history, 3)
        if pace is None and m.get("long_hours_90d") is not None:
            pace = m["long_hours_90d"] / 90
        for w in WINDOWS:
            need = needed_per_day(have, w["hours"], (w["by"] - today).days)
            out.append(f"{w['hours']:,} h {w['label']}: have {have:,.1f}, need {num(need, 1)} h/day, "
                       f"pace {num(pace, 1)} h/day: {verdict(have, w['hours'], need, pace)}")
    if route in ("shorts", "both"):
        have = m.get("shorts_views_90d") or 0
        pace = have / 90  # the window's own daily average; a change in a rolling total is not a day's views
        for w in WINDOWS:
            need = needed_per_day(have, w["views"], (w["by"] - today).days, window=90)
            out.append(f"{w['views'] // 1_000_000}M qualified views/90d {w['label']}: have {have:,}, "
                       f"need {num(need, 0)}/day, pace {num(pace, 0)}/day: {verdict(have, w['views'], need, pace)}")
    return out


def merged(public_numbers: dict, exact: dict | None, now: datetime) -> dict:
    m = {"subs": public_numbers.get("subs"), "shorts_views_90d": public_numbers.get("shorts_views_listed"),
         "listed": public_numbers.get("shorts_views_listed") or 0, "long_hours_365d": None, "exact": False}
    if exact and not exact.get("error") and exact.get("at") and \
            now - datetime.fromisoformat(exact["at"]) < timedelta(hours=48):
        m |= {k: exact[k] for k in ("subs", "shorts_views_90d", "long_hours_365d", "long_hours_90d", "subs_net_28d",
                                    "views_28d", "through") if k in exact}
        m["exact"] = "analytics_error" not in exact
    return m
