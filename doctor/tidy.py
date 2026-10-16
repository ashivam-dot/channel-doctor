"""Closes a channel repo's `alert` issues once what they reported is over, so a stale alert never hides a new one.

Run-failure alerts ("Studio run failed", "Promote failed (...)") and upload-gap alerts ("No upload in the last
30 hours"): fleet and "needs attention" alerts report something only the owner can settle (billing, a held run).
A run-failure alert is over when it has been quiet for 6 h, no watched workflow's latest finished run failed, a
scheduled watched workflow succeeded after the alert's last update, and the channel published after it. An
upload-gap alert is over once the channel published after its last update (Pangaea's health.yml opens it but
never closes it).

It also starts the doctor on a green run when the channel is silent, since a run that "succeeds" without
publishing never triggers a heal."""
from __future__ import annotations

import re
import urllib.error
from datetime import datetime, timezone

from . import config
from .github import GitHub
from .watch import feed, hours, silence

QUIET_H = 6
RUN_FAILED = re.compile(r"\bfailed\b", re.I)
NO_UPLOAD = re.compile(r"^No upload\b", re.I)


def stale(gh: GitHub, repo: str, lane: dict, ch: dict, at: datetime) -> list[tuple[dict, str]]:
    """[(issue, reason it is over)] for the repo's open run-failure and upload-gap alert issues."""
    issues = [i for i in gh.get(f"/repos/{repo}/issues", state="open", labels="alert", per_page=50) or []
              if "pull_request" not in i and (RUN_FAILED.search(i.get("title") or "") or NO_UPLOAD.search(i.get("title") or ""))]
    if not issues:
        return []
    last_upload = None
    if ch.get("youtube"):
        try:
            vids = feed(gh, ch["youtube"])
            last_upload = vids[0]["published"] if vids else None
        except RuntimeError:
            return []
    out = []
    for issue in [i for i in issues if NO_UPLOAD.search(i["title"])]:
        if last_upload and hours(last_upload, at) < hours(issue["updated_at"], at):
            out.append((issue, f"the channel published {hours(last_upload, at):.0f} h ago, after this alert"))
    issues = [i for i in issues if not NO_UPLOAD.search(i["title"])]
    if not issues:
        return out
    latest: dict[str, dict | None] = {}
    for wf in lane.get("workflows", {}):
        runs = gh.get(f"/repos/{repo}/actions/workflows/{wf}/runs", per_page=10).get("workflow_runs", [])
        latest[wf] = next((r for r in runs if r.get("conclusion") in ("success", "failure", "timed_out")), None)
    failing = [wf for wf, r in latest.items() if r and r["conclusion"] != "success"]
    for issue in issues:
        updated = issue["updated_at"]
        if failing or hours(updated, at) < QUIET_H:
            continue
        green = [wf for wf, r in latest.items() if r and lane["workflows"][wf].get("max_h")
                 and r["created_at"] > updated]
        if not green or (ch.get("youtube") and (not last_upload or hours(last_upload, at) > hours(updated, at))):
            continue
        out.append((issue, f"{', '.join(green)} succeeded since and the channel published "
                           f"{hours(last_upload, at):.0f} h ago" if last_upload else f"{', '.join(green)} succeeded since"))
    return out


def silent_run(gh: GitHub, repo: str, lane: dict, ch: dict, at: datetime) -> tuple[dict, str] | None:
    """(latest green run of the lane's producing workflow, why the channel counts as silent), or None."""
    if lane.get("mode") not in ("fix", "heal") or ch.get("hands_off") or ch.get("drill") or not ch.get("youtube"):
        return None
    producer = next((wf for wf, w in lane.get("workflows", {}).items() if w.get("max_h")), None)
    if not producer:
        return None
    try:
        gap = silence(ch, feed(gh, ch["youtube"]), at)
    except RuntimeError:
        return None
    if not gap:
        return None
    runs = gh.get(f"/repos/{repo}/actions/workflows/{producer}/runs", per_page=10).get("workflow_runs", [])
    run = next((r for r in runs if r.get("conclusion") == "success"), None)
    return (run, gap[1]) if run else None


def run_repo(repo: str, dry: bool = False) -> list[str]:
    cfg = config.load()
    _, ch, lane = config.lane_for(cfg, repo)
    gh, done = GitHub(), []
    at = datetime.now(timezone.utc)
    if found := silent_run(gh, repo, lane, ch, at):
        run, why = found
        print(f"silent: {why}; asking the doctor to heal green run {run['id']}")
        if not dry:
            try:
                gh.post(f"/repos/{repo}/actions/workflows/doctor.yml/dispatches",
                        {"ref": gh.get(f"/repos/{repo}")["default_branch"], "inputs": {"mode": "heal", "run_id": str(run["id"])}})
            except urllib.error.HTTPError as e:
                print(f"could not start the doctor (HTTP {e.code})")
    for issue, why in stale(gh, repo, lane, ch, at):
        done.append(f"#{issue['number']} {issue['title']}: {why}")
        if not dry:
            gh.post(f"/repos/{repo}/issues/{issue['number']}/comments",
                    {"body": f"Resolved, closed by Channel Doctor: {why}."})
            gh.patch(f"/repos/{repo}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
    print("tidy:\n  " + "\n  ".join(done or ["no stale alerts"]))
    return done
