"""Growth lab: keeps an eye on every channel's views and runs small, measured, policy-safe growth experiments.

Central (inside the 15-minute watch, public feeds only): **breakouts**, a fresh upload pulling at least 3x the
channel's usual views, are sent to Telegram once with what to do while the topic is hot.

Inside a channel repo (the daily growth job, with that channel's own YouTube credential), the experiments listed in
the channel's `lab` entry in channels.json, never on a hands-off channel:
- `comment`: one first comment on each new public upload, a genuine question for viewers in the channel's language
  (comments are a ranking signal and the start of a community; no links, tags or begging).
- `localize`: translated titles and descriptions (YouTube `localizations`) so viewers in other languages see the
  video in theirs and search finds it there; the original description (credits, licences) is kept below.
- `retitle`: an evergreen upload that stalled (48-120 h old, under 40% of the channel's median views) gets one new
  title, phrased with real YouTube search suggestions; after 72 h its views per day are compared with before, and the
  experiment switches itself off for the channel if fewer than a third of six tries helped.
Never fake engagement (views, likes, subscribers, sub4sub), never a misleading title, never spam: those are what
get a channel refused at YPP review or terminated.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://www.googleapis.com/youtube/v3"
LAB_TITLE = "Doctor lab"
LAB_INTRO = ("Channel Doctor's growth lab for this channel: which experiments ran on which uploads and what they "
             "measured. Edited by the daily growth job; switch an experiment off by removing it from `lab` in "
             "channel-doctor's channels.json.")
LANG_NAMES = {"hi": "Hindi (Devanagari script)", "en": "English", "es": "Spanish", "pt": "Portuguese (Brazil)",
              "de": "German", "fr": "French", "ar": "Arabic", "id": "Indonesian", "ja": "Japanese"}
BREAKOUT_X = 3.0
BREAKOUT_MIN_VIEWS = 200
MAX_COMMENTS, MAX_LOCALIZE, MAX_RETITLE = 3, 3, 1
RETITLE_JUDGE_H = 72


def hours(stamp: str, at: datetime) -> float:
    return (at - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600


# ---- central: breakouts from the public feeds --------------------------------------------------------------
def breakouts(videos: dict[str, list[dict]], at: datetime, seen: dict) -> list[str]:
    """Messages for uploads under 72 h old with at least BREAKOUT_X times the channel's usual views; each once."""
    out = []
    for name, vids in videos.items():
        settled = [v["views"] for v in vids if v.get("views") is not None and v.get("published")
                   and hours(v["published"], at) >= 48]
        if len(settled) < 4:
            continue
        usual = max(statistics.median(settled), 1)
        for v in vids:
            if (v.get("views") is None or not v.get("published") or v["id"] in seen
                    or hours(v["published"], at) > 72 or v["views"] < BREAKOUT_MIN_VIEWS):
                continue
            x = v["views"] / usual
            if x >= BREAKOUT_X:
                seen[v["id"]] = {"at": at.isoformat(), "channel": name, "title": v["title"][:90], "views": v["views"],
                                 "x": round(x, 1)}
                out.append(f"BREAKOUT {name}: \"{v['title'][:80]}\" has {v['views']:,} views in "
                           f"{hours(v['published'], at):.0f} h, {x:.1f}x the channel's usual {usual:,.0f}.\n"
                           f"Do now: a follow-up on the same topic within 24 h (next angle, what happened next, the "
                           f"question viewers ask), and the same hook style in the next uploads.\nhttps://youtu.be/{v['id']}")
    for vid in [k for k, s in seen.items() if hours(s["at"], at) > 24 * 14]:
        seen.pop(vid)
    return out


# ---- inside a channel repo: the YouTube Data API ---------------------------------------------------------
class YouTube:
    def __init__(self, raw_credential: str):
        info = json.loads(raw_credential)
        body = urllib.parse.urlencode({"client_id": info["client_id"], "client_secret": info["client_secret"],
                                       "refresh_token": info["refresh_token"], "grant_type": "refresh_token"}).encode()
        with urllib.request.urlopen(info.get("token_uri") or "https://oauth2.googleapis.com/token", body, timeout=60) as r:
            grant = json.load(r)
        self._token = grant["access_token"]
        self.scopes = sorted(s.rsplit("/", 1)[-1] for s in grant.get("scope", "").split())
        self.units = 0

    @property
    def can_write(self) -> bool:
        return "youtube.force-ssl" in self.scopes or "youtube" in self.scopes

    def call(self, method: str, path: str, params: dict, body: dict | None = None, cost: int = 1) -> dict:
        self.units += cost
        req = urllib.request.Request(f"{API}/{path}?{urllib.parse.urlencode(params)}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def uploads(self, n: int = 50) -> list[dict]:
        ch = self.call("GET", "channels", {"part": "contentDetails", "mine": "true"})["items"][0]
        playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        items = self.call("GET", "playlistItems", {"part": "contentDetails", "playlistId": playlist, "maxResults": n})
        ids = [i["contentDetails"]["videoId"] for i in items.get("items", [])]
        if not ids:
            return []
        res = self.call("GET", "videos", {"part": "snippet,statistics,status,localizations", "id": ",".join(ids)})
        return res.get("items", [])


def suggestions(query: str, lang: str) -> list[str]:
    """What people type into YouTube search for this topic (public autocomplete, no key)."""
    url = ("https://suggestqueries.google.com/complete/search?" +
           urllib.parse.urlencode({"client": "firefox", "ds": "yt", "q": query[:80], "hl": lang}))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as r:
            return [s for s in json.loads(r.read().decode("utf-8", "replace"))[1]][:8]
    except Exception:
        return []


# ---- experiments -----------------------------------------------------------------------------------------
COMMENT = """You run the YouTube channel "{channel}" ({about}). Write the channel's own first comment under its new
video, in {language}. One or two short sentences: a genuine, specific question about this video's topic that viewers
will want to answer (their opinion, guess, or experience). No links, no hashtags, no @mentions, at most one emoji,
no request to like or subscribe, no claim that is not in the title or description. Under 180 characters.
If the video is about a death, an attack, a disaster or any tragedy, be respectful and sober instead: no emoji, no
playful question, never ask for guesses about victims or blame; ask what viewers want to understand about it, or
invite them to share respectful words.
Title: {title}
Description: {description}
Return JSON {{"comment": "..."}}."""

LOCALIZE = """Translate this YouTube video's title and the first paragraph of its description from {source} into
{language} for viewers who speak it. Keep every name, number and hashtag exactly; keep the meaning, no new claims, no
clickbait. Title under 95 characters.
Title: {title}
First paragraph: {para}
Return JSON {{"title": "...", "summary": "..."}}."""

RETITLE = """A video on the YouTube channel "{channel}" ({about}) is getting far fewer views than the channel's
others. Write one new title in {language} that makes the same promise more clearly and more intriguingly to someone
scrolling, using the words people really search (suggestions below) where they fit. Keep every fact true to the
description; no new claims, no shock bait, no ALL CAPS, no misleading promise; under 70 characters; keep these
hashtags at the end if any: {tags}. It must differ from the current title.
Current title: {title}
Description: {description}
YouTube search suggestions for this topic: {suggest}
Return JSON {{"title": "..."}}."""

BAD = re.compile(r"https?://|www\.|@\w|#\w")
LINK = re.compile(r"https?://|www\.|@\w")


def clean_comment(text: str | None) -> str | None:
    text = (text or "").strip().strip('"')
    if not text or len(text) > 220 or BAD.search(text) or re.search(r"subscri|सब्सक्राइब|लाइक", text, re.I):
        return None
    return text


def hashtags(title: str) -> list[str]:
    return re.findall(r"#\w+", title)


def keep_snippet(sn: dict, **change) -> dict:
    """The snippet fields videos.update must get back so nothing else is erased."""
    keep = {k: sn[k] for k in ("title", "description", "tags", "categoryId", "defaultLanguage",
                               "defaultAudioLanguage") if k in sn}
    return keep | change


def published(v: dict) -> str:
    return v["snippet"].get("publishedAt") or ""


def views(v: dict) -> int:
    return int(v.get("statistics", {}).get("viewCount", 0))


def run_experiments(yt: YouTube, brain, ch: dict, state: dict, at: datetime, dry: bool = False) -> list[str]:
    """Run this channel's experiments once; return the plain-language log lines."""
    lab = ch.get("lab") or {}
    kinds, lang = lab.get("experiments", []), lab.get("language", "en")
    log: list[str] = []
    if not kinds or ch.get("hands_off"):
        return log
    if not yt.can_write:
        return [f"experiments need the youtube.force-ssl scope; this sign-in has only {', '.join(yt.scopes)}"]
    vids = [v for v in yt.uploads() if v.get("status", {}).get("privacyStatus") == "public" and published(v)]
    about = ch.get("about", "")[:300]
    done = state.setdefault("done", {})

    def ask(prompt):
        return brain.ask_json(prompt, "fast", models=6, rounds=2, pause=15) if brain.ready else None

    if "comment" in kinds:
        for v in [v for v in vids if hours(published(v), at) <= 48 and v["id"] not in done.get("comment", {})][:MAX_COMMENTS]:
            sn = v["snippet"]
            text = clean_comment((ask(COMMENT.format(channel=ch["name"], about=about, language=LANG_NAMES.get(lang, lang),
                                                     title=sn["title"], description=sn.get("description", "")[:700])) or {}).get("comment"))
            if not text:
                continue
            if not dry:
                try:
                    yt.call("POST", "commentThreads", {"part": "snippet"},
                            {"snippet": {"videoId": v["id"], "topLevelComment": {"snippet": {"textOriginal": text}}}}, 50)
                except urllib.error.HTTPError as e:
                    log.append(f"comment on {v['id']} refused (HTTP {e.code}); comments may be off for it")
                    done.setdefault("comment", {})[v["id"]] = {"at": at.isoformat(), "error": e.code}
                    continue
            done.setdefault("comment", {})[v["id"]] = {"at": at.isoformat(), "text": text, "views": views(v)}
            log.append(f"first comment on \"{sn['title'][:60]}\": {text}")

    if "localize" in kinds:
        targets = [l for l in lab.get("languages", []) if l != lang]
        todo = [v for v in vids if hours(published(v), at) <= 24 * 7 and v["id"] not in done.get("localize", {})]
        for v in todo[:MAX_LOCALIZE]:
            sn, local = v["snippet"], dict(v.get("localizations") or {})
            desc = sn.get("description", "")
            para = desc.split("\n\n", 1)[0][:900]
            for target in targets:
                if target in local:
                    continue
                res = ask(LOCALIZE.format(source=LANG_NAMES.get(lang, lang), language=LANG_NAMES.get(target, target),
                                          title=sn["title"], para=para)) or {}
                title, summary = (res.get("title") or "").strip()[:100], (res.get("summary") or "").strip()
                if not title or LINK.search(title) or set(hashtags(sn["title"])) - set(hashtags(title)):
                    continue
                full = (summary + "\n\n—\n" + desc) if summary else desc
                local[target] = {"title": title, "description": full.encode()[:4900].decode("utf-8", "ignore")}
            added = sorted(set(local) - set(v.get("localizations") or {}))
            if not added:
                continue
            if not dry:
                yt.call("PUT", "videos", {"part": "snippet,localizations"},
                        {"id": v["id"], "snippet": keep_snippet(sn, defaultLanguage=sn.get("defaultLanguage") or lang),
                         "localizations": local}, 50)
            done.setdefault("localize", {})[v["id"]] = {"at": at.isoformat(), "languages": added}
            log.append(f"translated title and description of \"{sn['title'][:60]}\" into {', '.join(added)}")

    if "retitle" in kinds and not state.get("retitle_off"):
        log += retitle(yt, ask, ch, vids, state, at, lang, about, dry)
    return log


def retitle(yt, ask, ch, vids, state, at, lang, about, dry) -> list[str]:
    log, tries = [], state.setdefault("retitles", [])
    by_id = {v["id"]: v for v in vids}
    for t in tries:
        if "after_per_day" not in t and hours(t["at"], at) >= RETITLE_JUDGE_H and t["id"] in by_id:
            gained = views(by_id[t["id"]]) - t["views_before"]
            t["after_per_day"] = round(gained / (hours(t["at"], at) / 24), 1)
            t["won"] = t["after_per_day"] >= t["before_per_day"]
            log.append(f"retitle result \"{t['new'][:50]}\": {t['before_per_day']}/day before, {t['after_per_day']}/day "
                       f"after ({'helped' if t['won'] else 'did not help'})")
    judged = [t for t in tries if "won" in t]
    if len(judged) >= 6 and sum(t["won"] for t in judged) * 3 < len(judged):
        state["retitle_off"] = f"{sum(t['won'] for t in judged)} of {len(judged)} retitles helped"
        return log + [f"retitle switched off for {ch['name']}: {state['retitle_off']}"]
    settled = [views(v) for v in vids if hours(published(v), at) >= 48]
    if len(settled) < 6:
        return log
    median = statistics.median(settled)
    tried = {t["id"] for t in tries}
    stalled = [v for v in vids if 48 <= hours(published(v), at) <= 120 and v["id"] not in tried
               and views(v) < 0.4 * median]
    for v in stalled[:MAX_RETITLE]:
        sn = v["snippet"]
        topic = re.sub(r"#\w+|[|:!?।]", " ", sn["title"]).strip()
        res = ask(RETITLE.format(channel=ch["name"], about=about, language=LANG_NAMES.get(lang, lang),
                                 tags=" ".join(hashtags(sn["title"])) or "none", title=sn["title"],
                                 description=sn.get("description", "")[:700],
                                 suggest="; ".join(suggestions(topic, lang)) or "none")) or {}
        new = (res.get("title") or "").strip()[:100]
        if not new or new == sn["title"] or new.isupper() or set(hashtags(sn["title"])) - set(hashtags(new)):
            continue
        if not dry:
            yt.call("PUT", "videos", {"part": "snippet"}, {"id": v["id"], "snippet": keep_snippet(sn, title=new)}, 50)
        h = hours(published(v), at)
        tries.append({"id": v["id"], "at": at.isoformat(), "old": sn["title"], "new": new, "views_before": views(v),
                      "before_per_day": round(views(v) / (h / 24), 1)})
        log.append(f"retitled a stalled upload ({views(v)} views vs the usual {median:.0f}): \"{sn['title'][:50]}\" -> \"{new}\"")
    return log


# ---- the daily job inside a channel repo -----------------------------------------------------------------
def run_repo(repo: str, cfg: dict, dry: bool = False) -> list[str]:
    from . import config, telegram
    from .brain import Brain
    from .github import GitHub
    from .state import IssueState, iso

    _, ch, _ = config.lane_for(cfg, repo)
    raw = os.environ.get("YOUTUBE_OAUTH_JSON")
    if not (ch.get("lab") or {}).get("experiments") or ch.get("hands_off") or not raw:
        print("no lab experiments for this channel")
        return []
    st = IssueState(GitHub(), repo, LAB_TITLE, "doctor", LAB_INTRO, dry)
    yt = YouTube(raw)
    at = datetime.now(timezone.utc)
    log = run_experiments(yt, Brain(), ch, st.data, at, dry)
    st.data["scopes"], st.data["ran"], st.data["units"] = yt.scopes, iso(at), yt.units
    st.save()
    print("lab:\n  " + "\n  ".join(log or ["nothing to do"]))
    if log and not dry:
        telegram.send(f"LAB {ch['name']}\n" + "\n".join(f"- {l}" for l in log))
    return log
