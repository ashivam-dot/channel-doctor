"""Twice-weekly channel review from public numbers: what is working, what is not, and evidence-backed changes.
Writes reports/review-<date>.md and reports/review-latest.json (read by each repo's improve run) and sends a summary."""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime

from . import config, telegram
from .brain import Brain
from .github import GitHub
from .state import iso, now
from .watch import feed, hours

HOME = os.environ.get("GITHUB_REPOSITORY", "ashivam-dot/channel-doctor")

PROMPT = """You review one automated YouTube channel twice a week for its owner. Be concrete and evidence-led.
Rules: never promise views or earnings; a pattern needs at least 4 videos on each side before you call it proven,
otherwise label it a hypothesis; respect the channel rules (CHANNEL.md excerpt) and YouTube policy (no faked real
people or events, no misleading titles); prefer changes the automation can apply itself.
Mark a recommendation auto_ok=true only if it is a small, low-risk change to the channel's own code (target "code"),
for example a title template, a timing window, a topic weight, a caption style, and it does not touch credentials,
safety checks or fact checks. Everything else is auto_ok=false.{hands_off}
Return JSON only:
{{"health": "one sentence", "working": ["..."], "not_working": ["..."],
  "recommendations": [{{"change": "imperative, specific", "evidence": "numbers from the data", "target": "code|titles|timing|topics|format|ops",
                        "risk": "low|medium|high", "auto_ok": false}}]}}
At most 4 recommendations, most valuable first.

Channel: {name}
About: {about}
Recent uploads (newest first: hours since publish, views, title):
{uploads}
Summary: {summary}
Automation reliability (last 7 days): {reliability}
Channel's own learnings file (excerpt):
{learnings}
CHANNEL.md excerpt:
{rules}
"""


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


def main(dry: bool = False):
    cfg, gh, brain, at = config.load(), GitHub(), Brain(), now()
    report = {"at": iso(at), "channels": {}}
    md = [f"# Channel review {at:%Y-%m-%d}", ""]
    tg = [f"Channel review {at:%d %b}"]
    for key, ch in cfg["channels"].items():
        if ch.get("drill"):
            continue
        vids = []
        if ch.get("youtube"):
            try:
                vids = feed(gh, ch["youtube"])
            except Exception as exc:
                print(f"{ch['name']}: feed unreadable ({type(exc).__name__})")
        table, summary = uploads_table(vids, at)
        learn, rules = learnings(gh, cfg["owner"], ch)
        prompt = PROMPT.format(name=ch["name"], about=ch.get("about", ""), uploads=table, summary=summary,
                               reliability=reliability(gh, cfg["owner"], ch, at), learnings=learn, rules=rules,
                               hands_off="\nThis channel is hands-off: every recommendation is auto_ok=false, written advice only." if ch.get("hands_off") else "")
        print(f"reviewing {ch['name']} ({len(vids)} uploads in feed)")
        res = brain.ask_json(prompt, "deep", models=6, rounds=2, pause=20) or {}
        print(f"  -> {'ok via ' + str(brain.used) if res else 'no answer'}")
        if ch.get("hands_off") or not any(l.get("mode") == "fix" for l in ch.get("lanes", [])):
            for r in res.get("recommendations", []):
                r["auto_ok"] = False
        report["channels"][key] = {"name": ch["name"], "summary": summary, **res}
        md += [f"## {ch['name']}", "", f"_{summary}_", "", f"**Health:** {res.get('health', 'no answer from the model')}", ""]
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
        tg += [f"  - {r.get('change')}{' (doctor will apply)' if r.get('auto_ok') else ''}" for r in recs[:2]]
    md.append(f"_Model: {brain.used or 'none'}_")
    text = "\n".join(md)
    print(text)
    if dry:
        return
    gh.put_file(HOME, f"reports/review-{at:%Y-%m-%d}.md", text, f"review {at:%Y-%m-%d}")
    gh.put_file(HOME, "reports/review-latest.json", json.dumps(report, indent=1, ensure_ascii=False), f"review {at:%Y-%m-%d} data")
    tg.append(f"\nFull report: https://github.com/{HOME}/blob/main/reports/review-{at:%Y-%m-%d}.md")
    telegram.send("\n".join(tg))
