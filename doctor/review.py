"""Twice-weekly channel review from public numbers: what is working, what is not, and evidence-backed changes.
Writes reports/review-<date>.md and reports/review-latest.json (read by each repo's improve run) and sends a summary."""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from datetime import datetime

from . import config, growth, mission, telegram
from .brain import Brain
from .github import GitHub
from .state import IssueState, iso, now
from .watch import feed, hours, title_problems

HOME = os.environ.get("GITHUB_REPOSITORY", "ashivam-dot/channel-doctor")

PROMPT = """{mission}

You review one automated YouTube channel twice a week for its owner, against the mission above: the fastest safe
route into YPP. Be concrete and evidence-led.
Rules: never promise views or earnings; a pattern needs at least 4 videos on each side before you call it proven,
otherwise label it a hypothesis; respect the channel rules (CHANNEL.md excerpt) and YouTube policy (no faked real
people or events, no misleading titles); prefer changes the automation can apply itself; rank recommendations by how
much they shorten the time to the next finish line (subscriber conversion is usually the binding constraint).
Judge the channel as a YPP reviewer would: could its videos be called template, repetitive, shock-bait or reused?
Mark a recommendation auto_ok=true only if it is a small, low-risk change to the channel's own code (target "code"),
for example a title template, a timing window, a topic weight, a caption style, a subscribe prompt, and it does not
touch credentials, safety checks or fact checks. Everything else is auto_ok=false.{hands_off}
Return JSON only:
{{"health": "one sentence", "working": ["..."], "not_working": ["..."],
  "ypp_lever": "the single change that most shortens this channel's time to its next finish line, and why",
  "inauthentic_risk": {{"level": "low|medium|high", "why": "what a YPP reviewer would flag, and the fix"}},
  "recommendations": [{{"change": "imperative, specific", "evidence": "numbers from the data", "target": "code|titles|timing|topics|format|ops",
                        "risk": "low|medium|high", "auto_ok": false}}]}}
At most 4 recommendations, most valuable first.

Channel: {name}
About: {about}
Road to YPP: {road}
Recent uploads (newest first: hours since publish, views, title):
{uploads}
Summary: {summary}
Results of earlier automatic improvements (median views of videos with 24 h+ of data, before vs after):
{experiments}
Automation reliability (last 7 days): {reliability}
Title and tag lint (published titles; deterministic checks): {lint}
Channel's own learnings file (excerpt):
{learnings}
CHANNEL.md excerpt:
{rules}
"""

FOCUS = """{mission}

These are this week's reviews of all the owner's channels with their Road-to-YPP numbers. Decide where effort should
go so that at least one channel reaches a finish line as early as possible before 31 January 2027, without policy risk.
Return JSON only:
{{"summary": "two sentences: which channel is closest to earning and what stands in the way",
  "focus": [{{"channel": "name", "why": "evidence", "next_step": "the concrete step this week"}}],
  "deprioritise": [{{"channel": "name", "why": "evidence"}}]}}
At most 3 focus entries, closest to earning first.

{channels}
"""


FEED_UNKNOWN = ("upload numbers unknown: YouTube's public feed could not be read in this run. This is NOT evidence "
                "of zero uploads or a stalled channel; judge uploads only from the workflow reliability below")
MIN_SCORED = 12  # videos with 24 h+ of views before any change may be applied automatically
RETRY_PAUSE_S = 60  # free-tier Gemini limits are per minute


def scored(vids: list[dict], at: datetime) -> int:
    return sum(1 for v in vids if v["views"] is not None and hours(v["published"], at) >= 24)


def uploads_table(vids: list[dict], at: datetime) -> tuple[str, str]:
    rows, views = [], []
    for v in vids[:15]:
        age = hours(v["published"], at)
        rows.append(f"{age:6.0f} h | {v['views'] if v['views'] is not None else '?':>7} | {v['title']}")
        if v["views"] is not None and age >= 24:
            views.append(v["views"])
    week = sum(1 for v in vids if hours(v["published"], at) <= 168)
    summary = f"{week} uploads in the last 7 days (feed shows the latest 15)"
    if views:
        summary += f"; videos older than 24 h: median {statistics.median(views):.0f} views, best {max(views)}, worst {min(views)}"
    return "\n".join(rows) or "(no uploads)", summary


def reliability(gh: GitHub, owner: str, ch: dict, at: datetime) -> str:
    out = []
    for lane in ch.get("lanes", []):
        for wf_file, wf in lane.get("workflows", {}).items():
            try:
                runs = gh.get(f"/repos/{owner}/{lane['repo']}/actions/workflows/{wf_file}/runs", per_page=100)["workflow_runs"]
            except Exception:
                continue
            week = [r for r in runs if hours(r["created_at"], at) <= 168 and r["status"] == "completed" and r["conclusion"] != "skipped"]
            if week:
                bad = sum(r["conclusion"] in ("failure", "timed_out") for r in week)
                out.append(f"{wf['name']}: {bad} of {len(week)} runs failed")
    return "; ".join(out) or "no workflow data"


# A share can't pass 100% (a change can: say "rise"), and a 0.0 is a missing number more often than a real one.
PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s?(?:%|percent\b)", re.I)
ZERO = re.compile(r"(?<![\d.,])0[.,]0+(?!\d)")


def title_flags(title: str) -> list[str]:
    out = title_problems(title)
    if over := [p for p in PERCENT.findall(title or "") if float(p.replace(",", ".")) > 100]:
        out.append(f"percentage over 100% ({over[0]}%)")
    if ZERO.search(title or ""):
        out.append(f"zero value {ZERO.search(title).group(0)!r}")
    return out


def lint(vids: list[dict], analytics: dict | None) -> list[str]:
    """Defects in published titles (public feed, and the channel's own API listing when it has one) and pipeline tags
    leaked into public video tags or channel keywords (the API shows tags to the owner's sign-in only)."""
    analytics = analytics or {}
    titles = {v["id"]: v["title"] for v in vids if v.get("id")}
    for vid, title, *_ in analytics.get("recent") or []:
        titles.setdefault(vid, title)
    out = [f"{vid} \"{t}\": {p}" for vid, t in titles.items() for p in title_flags(t)]
    tagged = [r for r in analytics.get("recent") or [] if len(r) > 3 and r[3]]
    if tagged:
        out.append(f"{len(tagged)} of the latest {len(analytics['recent'])} public uploads carry pipeline tags in their "
                   f"public tags (e.g. {tagged[0][0]}: {', '.join(tagged[0][3])})")
    if analytics.get("keywords_machine"):
        out.append(f"channel keywords carry pipeline tags: {', '.join(analytics['keywords_machine'][:5])}")
    return out


def learnings(gh: GitHub, owner: str, ch: dict) -> tuple[str, str]:
    lanes = ch.get("lanes", [])
    if not lanes:
        return "(none)", "(none)"
    repo = lanes[0]["repo"]
    rules = gh.text(f"https://raw.githubusercontent.com/{owner}/{repo}/main/CHANNEL.md") or "(none)"
    spec = lanes[0].get("learnings")
    learn = "(none)"
    if spec:
        branch, path = spec.split(":", 1)
        learn = gh.text(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}") or "(none)"
    return learn[:3000], rules[:3000]


def median_views(vids: list[dict], at: datetime, after: datetime | None = None, before: datetime | None = None):
    pick = [v["views"] for v in vids if v["views"] is not None and hours(v["published"], at) >= 24
            and (after is None or datetime.fromisoformat(v["published"]) > after)
            and (before is None or datetime.fromisoformat(v["published"]) <= before)]
    return (statistics.median(pick), len(pick)) if pick else (None, 0)


def experiments(gh: GitHub, owner: str, ch: dict, vids: list[dict], at: datetime) -> str:
    out = []
    for lane in ch.get("lanes", []):
        if lane.get("mode") != "fix":
            continue
        try:
            log = IssueState(gh, f"{owner}/{lane['repo']}", "Doctor log", "doctor", "", dry=True).data
        except Exception:
            continue
        for f in log.get("fixes", [])[-6:]:
            if f.get("workflow") != "*":
                continue
            t = datetime.fromisoformat(f["at"])
            b, nb = median_views(vids, at, before=t)
            a, na = median_views(vids, at, after=t)
            verdict = ("too early" if na < 4 or nb < 4 else
                       "helped" if a > b * 1.15 else "hurt" if a < b * 0.85 else "no clear change")
            out.append(f"- {f['at'][:10]} \"{f['summary']}\" ({f.get('status')}): before {b} over {nb} videos, "
                       f"after {a} over {na} videos -> {verdict}")
    return "\n".join(out) or "(none yet)"


def road_line(info: dict) -> str:
    if not info:
        return "no numbers yet"
    pub = info.get("public") or {}
    ana = info.get("analytics") or {}
    subs = info.get("subs") or 0
    views = ana.get("views_total") if info.get("exact") else (pub.get("shorts_views_listed", 0) + pub.get("long_views_listed", 0))
    conv = f"{1000 * subs / views:.1f} subscribers per 1,000 views" if views else "conversion unknown"
    parts = [f"{subs} subscribers ({conv})", f"route {info.get('route')}", f"{info.get('days_to_deadline')} days to the deadline"]
    if info.get("subs_per_day") is not None:
        parts.append(f"{info['subs_per_day']:.1f} subscribers/day")
    for tier in ("expanded", "full"):
        eta = info.get(f"{tier}_sub_eta_days")
        parts.append(f"{tier} sub line {'reached' if eta == 0 else 'unknown pace' if eta is None else f'~{eta:.0f} days away'}")
    if info.get("exact"):
        parts.append(f"exact: Shorts views 90d {ana.get('shorts_views_90d')}, long-form hours 365d {ana.get('long_hours_365d')}, "
                     f"net subscribers 28d {ana.get('subs_net_28d')}")
    else:
        parts.append("Shorts-views and watch-hour totals unavailable (public numbers only)")
    if info.get("projection"):
        parts.append("go/no-go: " + "; ".join(info["projection"]))
    return "; ".join(parts)


def main(dry: bool = False):
    cfg, gh, brain, at = config.load(), GitHub(), Brain(), now()
    report = {"at": iso(at), "channels": {}}
    md = [f"# Channel review {at:%Y-%m-%d}", ""]
    tg = [f"Channel review {at:%d %b}"]
    central = IssueState(gh, HOME, "Doctor state", "doctor", "", dry=True).data
    road = (central.get("road") or {}).get("info") or {}
    if not road:
        _, road, _ = growth.snapshot(gh, cfg, json.loads(json.dumps(central)), at)
    brief = mission.brief()
    focus_in = []
    for key, ch in cfg["channels"].items():
        if ch.get("drill"):
            continue
        vids, blind = [], False
        if ch.get("youtube"):
            try:
                vids = feed(gh, ch["youtube"])
            except Exception as exc:
                blind = True
                print(f"{ch['name']}: feed unreadable ({type(exc).__name__})")
        table, summary = uploads_table(vids, at)
        analytics = road.get(key, {}).get("analytics")
        if blind:
            table, summary = "(feed unreadable in this run)", FEED_UNKNOWN
        if not ch.get("youtube"):
            table, summary = "(no YouTube feed configured)", "upload numbers unknown: no public feed is configured for this channel"
        learn, rules = learnings(gh, cfg["owner"], ch)
        n_scored = scored(vids, at)
        notes = []
        if ch.get("hands_off"):
            notes.append("This channel is hands-off: every recommendation is auto_ok=false, written advice only.")
        if n_scored < MIN_SCORED:
            notes.append(f"Only {n_scored} videos have 24 h of data: too little for automatic changes, so every "
                         "recommendation is auto_ok=false, and call patterns hypotheses.")
        if ch.get("watch_from") and at < datetime.fromisoformat(ch["watch_from"]):
            notes.append("The channel (re)started publishing in the last day or two; YouTube's public feed lags for new "
                         "channels, so missing uploads in the feed are not evidence of a stall.")
        notes.append("A newer version of a video that replaced an older one (the older made private) is deliberate, not a duplicate.")
        rl = road_line(road.get(key, {}))
        flags = lint(vids, analytics)
        print(f"lint {ch['name']}: {len(flags)} title/tag flags")
        prompt = PROMPT.format(mission=brief, name=ch["name"], about=ch.get("about", ""), road=rl, uploads=table,
                               summary=summary, experiments=experiments(gh, cfg["owner"], ch, vids, at),
                               reliability=reliability(gh, cfg["owner"], ch, at), learnings=learn, rules=rules,
                               lint="; ".join(flags[:12]) or "nothing flagged",
                               hands_off="".join("\n" + n for n in notes))
        print(f"reviewing {ch['name']} ({len(vids)} uploads in feed)")
        res = brain.ask_json(prompt, "deep", models=6, rounds=2, pause=20) or {}
        if not res and brain.ready:
            time.sleep(RETRY_PAUSE_S)
            res = brain.ask_json(prompt, "deep", models=6, rounds=2, pause=20) or {}
        print(f"  -> {'ok via ' + str(brain.used) if res else 'no answer'}")
        locked = ch.get("hands_off") or n_scored < MIN_SCORED or not any(l.get("mode") == "fix" for l in ch.get("lanes", []))
        for r in res.get("recommendations", []):
            if locked or r.get("risk") != "low" or r.get("target") != "code":
                r["auto_ok"] = False
        report["channels"][key] = {"name": ch["name"], "summary": summary, "road": rl, "title_lint": flags, **res}
        risk = res.get("inauthentic_risk") or {}
        focus_in.append(f"## {ch['name']}\nRoad: {rl}\nUploads: {summary}\nHealth: {res.get('health')}\n"
                        f"Lever: {res.get('ypp_lever')}\nInauthentic-content risk: {risk.get('level')} ({risk.get('why')})")
        md += [f"## {ch['name']}", "", f"_{summary}_", "", f"**Road to YPP:** {rl}", "",
               f"**Health:** {res.get('health', 'no answer from the model')}", ""]
        if res.get("ypp_lever"):
            md += [f"**Biggest lever:** {res['ypp_lever']}", ""]
        if risk:
            md += [f"**Inauthentic-content risk:** {risk.get('level')} ({risk.get('why')})", ""]
        if flags:
            md += ["**Title and tag lint**", *[f"- {f}" for f in flags[:20]], ""]
        if res.get("working"):
            md += ["**Working**", *[f"- {w}" for w in res["working"]], ""]
        if res.get("not_working"):
            md += ["**Not working**", *[f"- {w}" for w in res["not_working"]], ""]
        for r in res.get("recommendations", []):
            md.append(f"- [{r.get('risk', '?')} risk, {'auto' if r.get('auto_ok') else 'manual'}] {r.get('change')} "
                      f"(evidence: {r.get('evidence')})")
        md.append("")
        recs = res.get("recommendations", [])
        tg.append(f"\n{ch['name']}: {res.get('health', 'no answer')}")
        if res.get("ypp_lever"):
            tg.append(f"  Lever: {res['ypp_lever']}")
        if risk.get("level") in ("medium", "high"):
            tg.append(f"  YPP risk ({risk['level']}): {risk.get('why')}")
        if flags:
            tg.append(f"  Title lint: {len(flags)} flagged, e.g. {flags[0]}")
        tg += [f"  - {r.get('change')}{' (doctor will apply)' if r.get('auto_ok') else ''}" for r in recs[:2]]

    focus = brain.ask_json(FOCUS.format(mission=brief, channels="\n\n".join(focus_in)), "deep", models=6, rounds=2, pause=20) or {}
    report["focus"] = focus
    if focus:
        md[2:2] = ["## Where to focus", "", focus.get("summary", ""), "",
                   *[f"- **{f.get('channel')}**: {f.get('next_step')} ({f.get('why')})" for f in focus.get("focus", [])],
                   *[f"- Deprioritise **{d.get('channel')}**: {d.get('why')}" for d in focus.get("deprioritise", [])], ""]
        tg[1:1] = ["\nROAD TO YPP: " + focus.get("summary", ""),
                   *[f"  Focus {f.get('channel')}: {f.get('next_step')}" for f in focus.get("focus", [])]]
    md.append(f"_Model: {brain.used or 'none'}_")
    text = "\n".join(md)
    print(text)
    if dry:
        return
    gh.put_file(HOME, f"reports/review-{at:%Y-%m-%d}.md", text, f"review {at:%Y-%m-%d}")
    gh.put_file(HOME, "reports/review-latest.json", json.dumps(report, indent=1, ensure_ascii=False), f"review {at:%Y-%m-%d} data")
    tg.append(f"\nFull report: https://github.com/{HOME}/blob/main/reports/review-{at:%Y-%m-%d}.md")
    telegram.send("\n".join(tg))
