"""Twice-weekly tool scout: new and rising open-source tools and models for the studios' needs, filtered to licences
the monetised channels may use, ranked by the model against the current stack."""
from __future__ import annotations

import json
import os
import urllib.parse
from datetime import timedelta

from . import config, mission, telegram
from .brain import Brain
from .github import GitHub
from .state import IssueState, iso, now, since

HOME = os.environ.get("GITHUB_REPOSITORY", "ashivam-dot/channel-doctor")
OK_LICENSES = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "mpl-2.0", "unlicense", "cc0-1.0",
               "cc-by-4.0", "cc-by-sa-4.0", "openrail", "openrail++", "creativeml-openrail-m", "zlib", "0bsd",
               "lgpl-3.0", "lgpl-2.1", "gpl-3.0", "agpl-3.0", "llama3.1", "llama3.2", "llama3.3", "gemma"}
SEEN_DAYS = 45


def license_ok(lic: str | None) -> bool:
    return bool(lic) and lic.lower() in OK_LICENSES


def github_candidates(gh: GitHub, query: str, at) -> list[dict]:
    out = []
    for q in (f"{query} created:>{(at - timedelta(days=120)):%Y-%m-%d} stars:>150",
              f"{query} pushed:>{(at - timedelta(days=21)):%Y-%m-%d} stars:>1500"):
        try:
            res = gh.get("/search/repositories", q=q, sort="stars", order="desc", per_page=8)
        except Exception as exc:
            print(f"github search failed for {q!r}: {type(exc).__name__}")
            continue
        for r in res.get("items", []):
            lic = (r.get("license") or {}).get("spdx_id")
            out.append({"id": f"gh:{r['full_name']}", "name": r["full_name"], "url": r["html_url"],
                        "about": (r.get("description") or "")[:200], "stars": r["stargazers_count"],
                        "license": lic, "updated": r["pushed_at"][:10]})
    return out


def hf_candidates(gh: GitHub, pipeline: str) -> list[dict]:
    raw = gh.text("https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"pipeline_tag": pipeline, "sort": "trendingScore", "direction": -1, "limit": 12}))
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    out = []
    for m in items:
        lic = next((t.split(":", 1)[1] for t in m.get("tags", []) if t.startswith("license:")), None)
        out.append({"id": f"hf:{m['id']}", "name": m["id"], "url": f"https://huggingface.co/{m['id']}",
                    "about": pipeline, "stars": m.get("likes", 0), "license": lic,
                    "updated": (m.get("lastModified") or m.get("createdAt") or "")[:10]})
    return out


PROMPT = """{mission}

You scout free tools for a set of automated YouTube studios that run on GitHub Actions CPU runners
(4 vCPU, 16 GB RAM, no GPU), Modal free credits, and a Mac. Current stack and needs:
{stack}

Candidates found this week (licence already filtered to ones safe for monetised use):
{cands}

Pick at most 6 that are realistically worth a test because they could beat what a channel uses now, ranked by how
much they would move a channel towards YPP (more original, varied, watchable videos; better subscriber conversion;
more reliable publishing). Prefer active,
well-documented projects that run on CPU or a free API. Skip anything needing paid APIs, a GPU at inference, or with
unclear provenance. Return JSON only:
{{"picks": [{{"name": "...", "url": "...", "for": "which channel and which step", "why": "what it could improve",
             "test": "a concrete 30-minute test to prove it", "risk": "licence/maintenance/quality notes"}}]}}"""


def main(dry: bool = False):
    cfg, gh, brain, at = config.load(), GitHub(), Brain(), now()
    st = IssueState(gh, HOME, "Scout memory", "doctor", "Tools the scout already reported (not repeated for 45 days).", dry)
    seen: dict = st.data.setdefault("seen", {})
    cands: dict[str, dict] = {}
    for need in cfg.get("scout", []):
        found = github_candidates(gh, need["github"], at) if need.get("github") else []
        found += hf_candidates(gh, need["hf"]) if need.get("hf") else []
        for c in found:
            if license_ok(c["license"]) and not (c["id"] in seen and since(seen[c["id"]], SEEN_DAYS * 24, at)):
                c["need"] = need["need"]
                cands.setdefault(c["id"], c)
    print(f"{len(cands)} licence-safe candidates")
    stack = "\n".join(f"- {ch['name']}: {ch.get('about', '')}" for ch in cfg["channels"].values())
    stack += "\nNeeds:\n" + "\n".join(f"- {n['need']}" for n in cfg.get("scout", []))
    stack += ("\nAlready in use (never suggest these again; a newer major version or a clearly better replacement "
              f"is fine): {cfg.get('in_use', '')}")
    listing = "\n".join(f"- {c['name']} ({c['url']}) | {c['license']} | {c['stars']} stars/likes | updated {c['updated']} | "
                        f"{c['need']} | {c['about']}" for c in list(cands.values())[:80])
    res = brain.ask_json(PROMPT.format(mission=mission.brief(3500), stack=stack, cands=listing or "(none)"), "deep") if cands else {}
    picks = (res or {}).get("picks", [])
    md = [f"# Tool scout {at:%Y-%m-%d}", "", f"{len(cands)} licence-safe candidates checked; model: {brain.used or 'none'}.", ""]
    for p in picks:
        md += [f"## [{p.get('name')}]({p.get('url')})", f"- For: {p.get('for')}", f"- Why: {p.get('why')}",
               f"- Test: {p.get('test')}", f"- Risk: {p.get('risk')}", ""]
    if not picks:
        md.append("Nothing worth testing this time.")
    text = "\n".join(md)
    print(text)
    if dry:
        return
    for c in cands:
        seen[c] = iso(at)
    st.data["seen"] = {k: v for k, v in seen.items() if since(v, SEEN_DAYS * 24, at)}
    st.save()
    gh.put_file(HOME, f"reports/scout-{at:%Y-%m-%d}.md", text, f"scout {at:%Y-%m-%d}")
    lines = [f"Tool scout {at:%d %b}: {len(picks)} worth testing"]
    lines += [f"- {p.get('name')}: {p.get('why')} ({p.get('for')})" for p in picks]
    lines.append(f"Report: https://github.com/{HOME}/blob/main/reports/scout-{at:%Y-%m-%d}.md")
    telegram.send("\n".join(lines))
