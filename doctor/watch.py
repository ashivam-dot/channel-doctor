"""Central fleet watch (every 15 min): uploads, schedules, failure streaks, open alerts, the doctors themselves.
Telegram gets new problems at once, reminders every 12 h, a note when they clear, and a daily digest."""
from __future__ import annotations

import os
import time
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from . import config, growth, telegram
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


def check(gh: GitHub, cfg: dict, at: datetime) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Return ({problem key: message}, {channel name: status lines})."""
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
                last = vids[0]["published"] if vids else None
                day = [v for v in vids if v["published"] and hours(v["published"], at) <= 24]
                if ch.get("watch_from") and at < datetime.fromisoformat(ch["watch_from"]):
                    lines.append(f"upload watch starts {ch['watch_from'][:16]} UTC")
                elif last is None:
                    problems[f"quiet:{key}"] = f"{ch['name']}: no public upload at all"
                elif hours(last, at) > ch.get("quiet_h", 36):
                    problems[f"quiet:{key}"] = (f"{ch['name']}: no new public upload for {hours(last, at):.0f} h "
                                                f"(limit {ch.get('quiet_h', 36)} h)")
                elif len(day) < ch.get("min_24h", 0):
                    problems[f"thin:{key}"] = (f"{ch['name']}: only {len(day)} public uploads in the last 24 h "
                                               f"(expected at least {ch['min_24h']}): the lane is green but "
                                               f"short of stories or refusing its own output")
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
                    problems[f"streak:{tag}"] = f"{ch['name']}: the last {STREAK} {tag} runs failed ({done[0]['html_url']})"
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


HELP = ("Channel Doctor commands:\n/status - current problems and every channel's state\n/digest - same as the daily digest\n"
        "/ypp - fresh Road to YPP numbers for every channel\n"
        "/review - run the channel review now\n/scout - run the tool scout now\n/help - this list")


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
    problems, status = check(gh, cfg, at)
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
    cmds, st.data["tg_offset"] = telegram.commands(int(st.data.get("tg_offset", 0)))
    cmds = list(dict.fromkeys(cmds))
    if due or "/ypp" in cmds or (st.data.get("road") or {}).get("day") is None:
        try:
            news = refresh_road(gh, cfg, st.data, at)
            if news and not dry:
                telegram.send("MILESTONE\n" + "\n".join(f"- {n}" for n in news))
        except Exception as exc:
            print(f"road to YPP failed: {type(exc).__name__}: {exc}")
    road = st.data.get("road")
    for c in cmds:
        if c in ("/status", "/digest", "/start"):
            telegram.send(digest(status, problems, at, road))
        elif c == "/ypp":
            telegram.send(road_text(road or {}))
        elif c in ("/review", "/scout"):
            try:
                if not dry:
                    gh.post(f"/repos/{HOME}/actions/workflows/{c[1:]}.yml/dispatches", {"ref": "main"})
                telegram.send(f"Started the {c[1:]}; results arrive here in a few minutes.")
            except urllib.error.HTTPError as e:
                telegram.send(f"Could not start the {c[1:]} (HTTP {e.code}).")
        else:
            telegram.send(HELP)

    if due:
        st.data["digest"] = today
        if not dry:
            telegram.send(digest(status, problems, at, road))
    st.data["checked"] = iso(at)
    st.save()
