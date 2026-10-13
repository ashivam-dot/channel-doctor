"""Closes a channel repo's `alert` issues once what they reported is over, so a stale alert never hides a new one.

An alert is over when it has been quiet for 6 h, no watched workflow's latest finished run failed, a scheduled
watched workflow succeeded after the alert's last update, and the channel published after it."""
from __future__ import annotations

from datetime import datetime, timezone

from . import config
from .github import GitHub
from .watch import feed, hours

QUIET_H = 6


def stale(gh: GitHub, repo: str, lane: dict, ch: dict, at: datetime) -> list[tuple[dict, str]]:
    """[(issue, reason it is over)] for the repo's open alert issues."""
    issues = [i for i in gh.get(f"/repos/{repo}/issues", state="open", labels="alert", per_page=50) or []
              if "pull_request" not in i]
    if not issues:
        return []
    latest: dict[str, dict | None] = {}
    for wf in lane.get("workflows", {}):
        runs = gh.get(f"/repos/{repo}/actions/workflows/{wf}/runs", per_page=10).get("workflow_runs", [])
        latest[wf] = next((r for r in runs if r.get("conclusion") in ("success", "failure", "timed_out")), None)
    failing = [wf for wf, r in latest.items() if r and r["conclusion"] != "success"]
    last_upload = None
    if ch.get("youtube"):
        try:
            vids = feed(gh, ch["youtube"])
            last_upload = vids[0]["published"] if vids else None
        except RuntimeError:
            return []
    out = []
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


def run_repo(repo: str, dry: bool = False) -> list[str]:
    cfg = config.load()
    _, ch, lane = config.lane_for(cfg, repo)
    gh, done = GitHub(), []
    for issue, why in stale(gh, repo, lane, ch, datetime.now(timezone.utc)):
        done.append(f"#{issue['number']} {issue['title']}: {why}")
        if not dry:
            gh.post(f"/repos/{repo}/issues/{issue['number']}/comments",
                    {"body": f"Resolved, closed by Channel Doctor: {why}."})
            gh.patch(f"/repos/{repo}/issues/{issue['number']}", {"state": "closed", "state_reason": "completed"})
    print("tidy:\n  " + "\n  ".join(done or ["no stale alerts"]))
    return done
