"""Central fleet watch (every 15 min): uploads, schedules, failure streaks, open alerts, the doctors themselves.
Telegram gets new problems at once, reminders every 12 h, a note when they clear, and a daily digest."""
from __future__ import annotations

import json
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from . import config, growth, lab, telegram
from .github import GitHub
from .state import IssueState, iso, now, since

NS = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/", "yt": "http://www.youtube.com/xml/schemas/2015"}
HOME = os.environ.get("GITHUB_REPOSITORY", "ashivam-dot/channel-doctor")
DIGEST_UTC_HOUR = 15.5  # 21:00 IST
STREAK = 3
REMIND_H = 12


def hours(stamp: str, at: datetime) -> float:
    return (at - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 3600


FEED_PAUSES_S = (0, 4, 12)


def feed(gh: GitHub, channel_id: str) -> list[dict]:
    """The channel's latest public uploads. YouTube's feed endpoint often fails for runner IPs for a while, so each
    try asks the channel feed and then the same uploads as a playlist feed (UC... -> UU...)."""
    urls = [f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"]
    if channel_id.startswith("UC"):
        urls.append(f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{channel_id[2:]}")
    raw = None
    for pause in FEED_PAUSES_S:
        time.sleep(pause)
        raw = next((r for r in (gh.text(u) for u in urls) if r and "<feed" in r), None)
        if raw:
            break
    if not raw:
        raise RuntimeError("feed unreadable")
    out = []
    for e in ET.fromstring(raw).findall("a:entry", NS):
        stats = e.find("m:group/m:community/m:statistics", NS)
        out.append({"id": e.findtext("yt:videoId", namespaces=NS), "title": e.findtext("a:title", namespaces=NS),
                    "published": e.findtext("a:published", namespaces=NS),
                    "views": int(stats.get("views", 0)) if stats is not None else None})
    return sorted(out, key=lambda v: v["published"] or "", reverse=True)


def silence(ch: dict, vids: list[dict], at: datetime) -> tuple[str, str] | None:
    """("quiet" or "thin", message) when the channel publishes less than it should, else None."""
    last = vids[0]["published"] if vids else None
    day = [v for v in vids if v["published"] and hours(v["published"], at) <= 24]
    if ch.get("watch_from") and at < datetime.fromisoformat(ch["watch_from"]):
        return None
    if last is None:
        return "quiet", f"{ch['name']}: no public upload at all"
    if hours(last, at) > ch.get("quiet_h", 36):
        return "quiet", f"{ch['name']}: no new public upload for {hours(last, at):.0f} h (limit {ch.get('quiet_h', 36)} h)"
    if len(day) < ch.get("min_24h", 0):
        return "thin", (f"{ch['name']}: only {len(day)} public uploads in the last 24 h (expected at least "
                        f"{ch['min_24h']}): the lane is green but short of stories or refusing its own output")
    return None


QA_HOURS = 72
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "of", "on", "or", "per", "the", "to",
         "vs", "via", "with", "than", "over", "under", "after", "before"}
LEFTOVER = re.compile(r"\b(?:None|null|undefined|NaN|TODO|TBD|lorem ipsum)\b|\{[a-z_]*\}|\\n|\s{2,}")


def title_problems(title: str) -> list[str]:
    """Defects a viewer would see in an uploaded title (Latin-script titles only)."""
    out = []
    t = (title or "").replace(" #shorts", "").strip()
    if not t:
        return ["empty title"]
    if LEFTOVER.search(t):
        out.append(f"template leftover {LEFTOVER.search(t).group(0)!r}")
    if t[0].isalpha() and t[0].islower():
        out.append("starts in lower case")
    words = [m.group(0) for m in re.finditer(r"[A-Za-z][A-Za-z'-]*", t)
             if m.start() > 0 and len(m.group(0)) > 3 and m.group(0).lower() not in SMALL]
    caps = [w for w in words if w[0].isupper()]
    lows = [w for w in words if w[0].islower()]
    if len(caps) >= 3 and len(lows) == 1:
        out.append(f"one word in lower case among title-case words ({lows[0]!r})")
    tokens = [w.strip(".,:;!?|\"'()").casefold() for w in t.split()]
    if dup := next((t.split()[i] for i in range(1, len(tokens)) if tokens[i] and tokens[i] == tokens[i - 1]), None):
        out.append(f"repeated word {dup!r}")
    return out


def code_changed(gh: GitHub, repo: str, lane: dict) -> str | None:
    """When the lane's editable code last changed on the default branch, or None (nothing editable, or unknown)."""
    dates = []
    for glob in lane.get("editable", []):
        path = glob.split("*", 1)[0].rstrip("/")
        try:
            commits = gh.get(f"/repos/{repo}/commits?" + urllib.parse.urlencode({"path": path, "per_page": 1}))
            dates.append(commits[0]["commit"]["committer"]["date"])
        except Exception:
            continue
    return max(dates) if dates else None


def quality(ch: dict, vids: list[dict], at: datetime, fixed_at: str | None = None) -> str | None:
    """What is visibly wrong with the channel's uploads of the last QA_HOURS (titles, duplicates), or None.
    Uploads from before `fixed_at` (the producing code's last change) are skipped: that code is already different."""
    recent = [v for v in vids if v.get("published") and hours(v["published"], at) <= QA_HOURS
              and not (fixed_at and hours(v["published"], at) > hours(fixed_at, at))]
    found = [f"\"{v['title']}\": {p}" for v in recent for p in title_problems(v["title"])]
    seen: dict[str, int] = {}
    for v in recent:
        key = re.sub(r"\W+", " ", (v["title"] or "").lower()).strip()
        seen[key] = seen.get(key, 0) + 1
    found += [f"\"{k}\" was uploaded {n} times" for k, n in seen.items() if k and n > 1]
    if fell := slump(vids, at, fixed_at):
        found.append(fell)
    return f"{ch['name']}: uploads with visible defects: " + "; ".join(found)[:600] if found else None


SLUMP_SHARE, SLUMP_MIN_USUAL, SLUMP_AGE_H = 0.1, 100, (12, 72)


def slump(vids: list[dict], at: datetime, fixed_at: str | None = None) -> str | None:
    """Viewers refusing what the channel now makes: its two newest uploads, both SLUMP_AGE_H old, under SLUMP_SHARE
    of the usual (median of uploads 48 h+ old, at least SLUMP_MIN_USUAL). Orbitwire's first two Shorts after a
    format change (2026-10-16) had 5 and 2 views against the usual 300: the change, not the topic, is the suspect."""
    dated = sorted((v for v in vids if v.get("published") and v.get("views") is not None),
                   key=lambda v: v["published"], reverse=True)
    settled = [v["views"] for v in dated if hours(v["published"], at) >= 48]
    if len(settled) < 6 or (usual := statistics.median(settled)) < SLUMP_MIN_USUAL or len(dated) < 2:
        return None
    newest = dated[:2]
    if fixed_at and hours(newest[0]["published"], at) > hours(fixed_at, at):
        return None
    lo, hi = SLUMP_AGE_H
    if all(lo <= hours(v["published"], at) <= hi and v["views"] < SLUMP_SHARE * usual for v in newest):
        return (f"the 2 newest uploads have {newest[0]['views']} and {newest[1]['views']} views against the usual "
                f"{usual:,.0f}: check what the producing code changed just before them and undo it if it is the cause")
    return None


def symptom(ch: dict, vids: list[dict], at: datetime, fixed_at: str | None = None) -> tuple[str, str] | None:
    """The channel's most urgent green-run problem: silence first, then visible defects in recent uploads."""
    if gap := silence(ch, vids, at):
        return gap
    if not ch.get("hands_off") and (bad := quality(ch, vids, at, fixed_at)):
        return "qa", bad
    return None


def never_started(gh: GitHub, repo: str, run: dict) -> bool:
    """A failed run whose jobs ran no step at all: GitHub refused to start it (billing or spending limit)."""
    try:
        jobs = gh.get(f"/repos/{repo}/actions/runs/{run['id']}/jobs")["jobs"]
    except (urllib.error.HTTPError, KeyError):
        return False
    return bool(jobs) and all(not j.get("steps") for j in jobs)


def check(gh: GitHub, cfg: dict, at: datetime,
          videos: dict[str, list[dict]] | None = None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return ({problem key: message}, {channel name: status lines}); `videos` collects each channel's feed."""
    problems: dict[str, str] = {}
    status: dict[str, list[str]] = {}
    owner = cfg["owner"]
    blind = 0
    for key, ch in cfg["channels"].items():
        if ch.get("drill"):
            continue
        lines = status.setdefault(ch["name"], [])
        if ch.get("youtube"):
            try:
                vids = feed(gh, ch["youtube"])
                if videos is not None:
                    videos[ch["name"]] = vids
                last = vids[0]["published"] if vids else None
                day = [v for v in vids if v["published"] and hours(v["published"], at) <= 24]
                if ch.get("watch_from") and at < datetime.fromisoformat(ch["watch_from"]):
                    lines.append(f"upload watch starts {ch['watch_from'][:16]} UTC")
                elif gap := symptom(ch, vids, at):
                    problems[f"{gap[0]}:{key}"] = gap[1]
                lines.append(f"uploads 24h: {len(day)}; last {hours(last, at):.1f} h ago" if last else "no uploads")
                if vids:
                    v = vids[0]
                    lines.append(f"latest: {v['title'][:70]} ({v['views'] if v['views'] is not None else '?'} views)")
            except Exception as exc:
                blind += 1
                lines.append(f"feed unreadable ({type(exc).__name__})")
        for lane in ch.get("lanes", []):
            repo = f"{owner}/{lane['repo']}"
            for wf_file, wf in lane.get("workflows", {}).items():
                base = f"/repos/{repo}/actions/workflows/{wf_file}"
                try:
                    state = gh.get(base)["state"]
                    runs = gh.get(f"{base}/runs", per_page=10)["workflow_runs"]
                except urllib.error.HTTPError as e:
                    lines.append(f"{lane['repo']}/{wf_file}: unreadable (HTTP {e.code})")
                    continue
                tag = f"{lane['repo']}/{wf_file}"
                if state != "active":
                    if wf.get("max_h"):
                        problems[f"disabled:{tag}"] = f"{ch['name']}: {tag} is {state}; its schedule no longer runs"
                    continue
                scheduled = [r for r in runs if r["event"] == "schedule"]
                if wf.get("max_h") and scheduled and hours(scheduled[0]["created_at"], at) > wf["max_h"]:
                    problems[f"late:{tag}"] = (f"{ch['name']}: {tag} has not run on schedule for "
                                               f"{hours(scheduled[0]['created_at'], at):.0f} h (limit {wf['max_h']} h)")
                done = [r for r in runs if r["status"] == "completed" and r["conclusion"] != "skipped"][:STREAK]
                if len(done) == STREAK and all(r["conclusion"] in ("failure", "timed_out") for r in done):
                    why = " - blocked by GitHub billing: the jobs never started" if never_started(gh, repo, done[0]) else ""
                    problems[f"streak:{tag}"] = f"{ch['name']}: the last {STREAK} {tag} runs failed{why} ({done[0]['html_url']})"
                if done:
                    lines.append(f"{wf['name']}: {done[0]['conclusion']} {hours(done[0]['updated_at'], at):.1f} h ago")
            try:
                docs = gh.get(f"/repos/{repo}/actions/workflows/doctor.yml/runs", per_page=4)["workflow_runs"]
                docs = [r for r in docs if r["status"] == "completed"][:2]
                if len(docs) == 2 and all(r["conclusion"] == "failure" for r in docs):
                    problems[f"doctor:{lane['repo']}"] = f"{ch['name']}: the doctor itself failed twice in {lane['repo']} ({docs[0]['html_url']})"
            except urllib.error.HTTPError:
                pass
            try:
                for issue in gh.get(f"/repos/{repo}/issues", state="open", labels="alert", per_page=10):
                    if "pull_request" in issue or issue["title"] == "Channel fleet needs attention":
                        continue
                    problems[f"issue:{lane['repo']}#{issue['number']}"] = (f"{ch['name']}: open alert \"{issue['title'][:80]}\" "
                                                                          f"{issue['html_url']}")
            except urllib.error.HTTPError:
                pass
    if blind and blind == sum(1 for c in cfg["channels"].values() if c.get("youtube")):
        problems["blind:feeds"] = "YouTube feeds answered for no channel: the upload watch is blind"
    return problems, status


def road_text(road: dict) -> str:
    if not road.get("lines"):
        return "Road to YPP: no numbers yet."
    days = (growth.DEADLINE - datetime.fromisoformat(road["day"]).date()).days
    return (f"Road to YPP ({road['day']}; {days} days left before the 1 Feb 2027 rules double the bar)\n" +
            "\n".join(f"- {l}" for l in road["lines"]))


def digest(status: dict[str, list[str]], problems: dict[str, str], at: datetime, road: dict | None = None) -> str:
    head = f"Channel Doctor daily digest, {at.astimezone(timezone.utc):%d %b %H:%M} UTC"
    body = [head, ""]
    if road:
        body += [road_text(road), ""]
    for name, lines in status.items():
        flag = "attention" if any(p.startswith(name + ":") for p in problems.values()) else "ok"
        body.append(f"{name} [{flag}]")
        body += [f"  {l}" for l in lines[:6]]
    body.append("")
    body.append(f"Open problems: {len(problems)}" + ("" if not problems else "\n" + "\n".join(f"- {p}" for p in problems.values())))
    return "\n".join(body)


HELP = ("Channel Doctor commands:\n/status - current problems and every channel's state\n"
        "/channel <name> - one channel in detail: latest uploads, workflows, problems (e.g. /channel atlas)\n"
        "/top - best uploads of the last 7 days across all channels\n"
        "/ask <question> - or just type a question: answered from the live fleet data, reviews and the mission\n"
        "/digest - same as the daily digest\n/ypp - fresh Road to YPP numbers for every channel\n"
        "/review - run the channel review now\n/scout - run the tool scout now\n/id - this chat's id\n/help - this list")
LISTEN_S = 13 * 60  # answer messages until the next 15-minute run is due
REVIEW = Path(__file__).resolve().parent.parent / "reports" / "review-latest.json"


def ago(stamp: str | None, at: datetime) -> str:
    if not stamp:
        return "?"
    h = hours(stamp, at)
    return f"{h:.1f} h ago" if h < 48 else f"{h / 24:.0f} d ago"


def find_channel(query: str, cfg: dict) -> dict | None:
    """A channel by key, name or a word of its name, case-insensitive."""
    q = query.strip().lower()
    if not q:
        return None
    chans = [c for c in cfg["channels"].values() if not c.get("drill")]
    for test in (lambda k, c: q in (k.lower(), c["name"].lower()),
                 lambda k, c: c["name"].lower().startswith(q) or k.lower().startswith(q),
                 lambda k, c: q in c["name"].lower() or q in k.lower()):
        hit = [c for k, c in cfg["channels"].items() if c in chans and test(k, c)]
        if hit:
            return hit[0]
    return None


def channel_text(query: str, cfg: dict, status: dict, problems: dict, videos: dict, at: datetime) -> str:
    ch = find_channel(query, cfg)
    if ch is None:
        names = ", ".join(c["name"] for c in cfg["channels"].values() if not c.get("drill"))
        return f"Which channel? Try /channel <name>. Channels: {names}"
    name = ch["name"]
    out = [f"{name} (as of {at:%H:%M} UTC)"]
    out += [f"- {l}" for l in status.get(name, [])]
    vids = videos.get(name) or []
    if vids:
        out.append("Latest uploads:")
        out += [f"- {v['views'] if v['views'] is not None else '?'} views, {ago(v['published'], at)}: {v['title'][:70]}"
                f"\n  https://youtu.be/{v['id']}" for v in vids[:5]]
    mine = [p for p in problems.values() if p.startswith(name + ":")]
    out.append("Problems: none" if not mine else "Problems:\n" + "\n".join(f"- {p}" for p in mine))
    review = (load_review().get("channels") or {})
    rv = next((r for r in review.values() if r.get("name") == name), None)
    if rv and rv.get("ypp_lever"):
        out.append(f"Last review's biggest lever: {rv['ypp_lever'][:300]}")
    return "\n".join(out)


def top_text(videos: dict, at: datetime, days: int = 7, n: int = 10) -> str:
    rows = [(v["views"], name, v) for name, vids in videos.items() for v in vids
            if v.get("views") is not None and v.get("published") and hours(v["published"], at) <= days * 24]
    if not rows:
        return f"No public uploads with view counts in the last {days} days."
    rows.sort(key=lambda r: r[0], reverse=True)
    lines = [f"Top uploads, last {days} days (feeds show each channel's latest 15):"]
    lines += [f"{i}. {views:,} views - {name} - {v['title'][:60]} ({ago(v['published'], at)})\n   https://youtu.be/{v['id']}"
              for i, (views, name, v) in enumerate(rows[:n], 1)]
    return "\n".join(lines)


def load_review() -> dict:
    try:
        return json.loads(REVIEW.read_text())
    except (OSError, ValueError):
        return {}


ASK = """You are Channel Doctor, the caretaker of the owner's automated YouTube channels, answering the owner on
Telegram. Answer the question from the facts below only; when they don't say, say so plainly and name what would
tell us. Be direct and specific (numbers, channel names, links), at most 12 short lines, plain text, no markdown.
Never promise views or earnings.

{mission}

{alternatives}

## Live fleet status ({at} UTC)
{digest}

## Uploads in the last 7 days
{top}

## Breakouts in the last 14 days (3x+ the channel's usual views)
{breakouts}

## Last channel review ({review_at})
{review}

## Question
{question}

Return JSON {{"answer": "..."}}."""


def review_brief(review: dict, limit: int = 5000) -> str:
    parts = []
    for r in (review.get("channels") or {}).values():
        parts.append(f"{r.get('name')}: health: {r.get('health', '')} | lever: {r.get('ypp_lever', '')} | "
                     f"not working: {'; '.join(r.get('not_working') or [])}")
    if review.get("focus"):
        parts.append(f"Portfolio focus: {json.dumps(review['focus'], ensure_ascii=False)}")
    return "\n".join(parts)[:limit] or "none"


def ask(question: str, ctx: dict, brain=None) -> str:
    if not question.strip():
        return "Ask me anything about the channels, e.g. \"why is Kahaani Chaos quiet?\" or \"which channel grows fastest?\""
    from .brain import Brain
    from .mission import alternatives, brief

    brain = brain or Brain()
    if not brain.ready:
        return "I can't answer free questions yet: DOCTOR_GEMINI_API_KEY is not set for the watch."
    review = load_review()
    prompt = ASK.format(mission=brief(3000), alternatives=alternatives(), at=f"{ctx['at']:%Y-%m-%d %H:%M}",
                        digest=digest(ctx["status"], ctx["problems"], ctx["at"], ctx.get("road")),
                        top=top_text(ctx["videos"], ctx["at"], n=15),
                        breakouts="\n".join(f"- {b['channel']}: {b['title']} ({b['views']} views, {b['x']}x)"
                                            for b in (ctx.get("breakouts") or {}).values()) or "none",
                        review_at=review.get("at", "none"),
                        review=review_brief(review), question=question[:1000])
    res = brain.ask_json(prompt, "fast", models=6, rounds=2, pause=10)
    answer = (res or {}).get("answer") if isinstance(res, dict) else None
    if not answer:
        return "Gemini didn't answer just now (busy or out of quota); try again in a few minutes, or use /status."
    return f"{str(answer).strip()[:3000]}\n\n(answered by {brain.used})"


def respond(cmd: str, rest: str, ctx: dict, gh: GitHub, cfg: dict, dry: bool = False) -> str:
    """The reply to one owner message; ctx holds this run's status, problems, videos, road and time."""
    at = ctx["at"]
    if cmd in ("/status", "/digest", "/start"):
        return digest(ctx["status"], ctx["problems"], at, ctx.get("road"))
    if cmd == "/ypp":
        return road_text(ctx.get("road") or {})
    if cmd in ("/channel", "/ch"):
        return channel_text(rest, cfg, ctx["status"], ctx["problems"], ctx["videos"], at)
    if cmd == "/top":
        days = int(rest) if rest.isdigit() and 1 <= int(rest) <= 30 else 7
        return top_text(ctx["videos"], at, days)
    if cmd == "/ask":
        return ask(rest, ctx)
    if cmd == "/id":
        return f"This chat's id: {os.environ.get('TELEGRAM_CHAT_ID', '?')}"
    if cmd in ("/review", "/scout"):
        try:
            if not dry:
                gh.post(f"/repos/{HOME}/actions/workflows/{cmd[1:]}.yml/dispatches", {"ref": "main"})
            return f"Started the {cmd[1:]}; results arrive here in a few minutes."
        except urllib.error.HTTPError as e:
            return f"Could not start the {cmd[1:]} (HTTP {e.code})."
    return HELP


def refresh_road(gh: GitHub, cfg: dict, data: dict, at: datetime) -> list[str]:
    lines, info, news = growth.snapshot(gh, cfg, data, at)
    data["road"] = {"day": f"{at:%Y-%m-%d}", "at": iso(at), "lines": lines, "info": info}
    print(road_text(data["road"]))
    return news


def main(dry: bool = False):
    cfg = config.load()
    gh = GitHub()
    at = now()
    st = IssueState(gh, HOME, "Doctor state", "doctor",
                    "Central Channel Doctor state (alerts sent, Telegram offset, last digest). Edited by the watch.", dry)
    alerts: dict = st.data.setdefault("alerts", {})
    videos: dict[str, list[dict]] = {}
    problems, status = check(gh, cfg, at, videos)
    seen = st.data.setdefault("breakouts", {})
    for msg in lab.breakouts(videos, at, seen):
        print(msg.splitlines()[0])
        if not dry:
            telegram.send(msg)
    print("problems:\n  " + "\n  ".join(problems.values()) if problems else "no problems")

    new, remind, cleared = [], [], []
    for k, msg in problems.items():
        a = alerts.get(k)
        if not a:
            alerts[k] = {"first": iso(at), "sent": iso(at), "msg": msg}
            new.append(msg)
        elif not since(a["sent"], REMIND_H, at):
            a["sent"] = iso(at)
            remind.append(f"{msg} (since {a['first'][:16].replace('T', ' ')} UTC)")
        else:
            a["msg"] = msg
    for k in [k for k in alerts if k not in problems]:
        cleared.append(alerts.pop(k)["msg"])
    parts = []
    if new:
        parts.append("NEW PROBLEM\n" + "\n".join(f"- {m}" for m in new))
    if remind:
        parts.append("STILL OPEN\n" + "\n".join(f"- {m}" for m in remind))
    if cleared:
        parts.append("RESOLVED\n" + "\n".join(f"- {m}" for m in cleared))
    if parts and not dry:
        telegram.send("\n\n".join(parts))

    today = f"{at:%Y-%m-%d}"
    due = at.hour + at.minute / 60 >= DIGEST_UTC_HOUR and st.data.get("digest") != today
    msgs, st.data["tg_offset"] = telegram.messages(int(st.data.get("tg_offset", 0)))
    msgs = list(dict.fromkeys(msgs))
    if due or any(c == "/ypp" for c, _ in msgs) or (st.data.get("road") or {}).get("day") is None:
        try:
            news = refresh_road(gh, cfg, st.data, at)
            if news and not dry:
                telegram.send("MILESTONE\n" + "\n".join(f"- {n}" for n in news))
        except Exception as exc:
            print(f"road to YPP failed: {type(exc).__name__}: {exc}")
    road = st.data.get("road")
    ctx = {"at": at, "status": status, "problems": problems, "videos": videos, "road": road, "breakouts": seen}
    for c, rest in msgs:
        telegram.send(respond(c, rest, ctx, gh, cfg, dry))

    if due:
        st.data["digest"] = today
        if not dry:
            telegram.send(digest(status, problems, at, road))
    menu = [list(m) for m in telegram.MENU]
    if st.data.get("menu") != menu and not dry and telegram.set_menu():
        st.data["menu"] = menu
    st.data["checked"] = iso(at)
    st.save()
    if not dry and os.environ.get("DOCTOR_LISTEN") == "1":
        listen(st, ctx, gh, cfg, time.monotonic() + LISTEN_S)


def listen(st: IssueState, ctx: dict, gh: GitHub, cfg: dict, until: float):
    """Long-poll Telegram until `until`, replying to each message as it arrives."""
    while (left := until - time.monotonic()) > 5:
        msgs, offset = telegram.messages(int(st.data.get("tg_offset", 0)), wait=int(min(50, left - 3)))
        if offset != st.data.get("tg_offset"):
            st.data["tg_offset"] = offset
            st.save()
        for c, rest in msgs:
            telegram.send(respond(c, rest, ctx, gh, cfg))
