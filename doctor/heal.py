"""Runs inside a channel repo (through the reusable workflow) when one of its workflows fails, and twice a week to
apply the central review's safe improvements."""
from __future__ import annotations

import base64
import json
import re
import urllib.error
from pathlib import Path

from . import config, fixer, mission, telegram, watch
from .brain import Brain
from .github import GitHub
from .playbook import classify, excerpt, fingerprint
from .state import IssueState, iso, now, since

# A health check that fails because nothing was published: the cause is in the producing workflow, not the check.
SILENT = re.compile(r"\bno (?:new |public )?uploads?\b|\bnothing (?:eligible|published|to publish)\b|\bsilent\b|"
                    r"\bmissed slot\b", re.I)

INTRO = ("Channel Doctor's log for this repo: every failure it looked at, what it did, and any fix it pushed. "
         "Set the repository variable `DOCTOR_PAUSED=true` to stop it, `DOCTOR_MODE=report` to stop code changes.")
REVIEW_URL = "https://raw.githubusercontent.com/ashivam-dot/channel-doctor/main/reports/review-latest.json"


class Healer:
    def __init__(self, repo: str, target: Path, dry: bool = False, report_only: bool = False):
        self.cfg = config.load()
        self.key, self.ch, lane = config.lane_for(self.cfg, repo)
        self.lane = dict(lane, channel=self.key)
        self.repo, self.target, self.dry = repo, target, dry
        self.can_edit = self.lane.get("mode") == "fix" and not self.ch.get("hands_off") and not report_only
        self.gh, self.brain = GitHub(), Brain()
        self.state = IssueState(self.gh, repo, "Doctor log", "doctor", INTRO, dry)
        self.events = self.state.data.setdefault("events", [])
        self.fixes = self.state.data.setdefault("fixes", [])

    # ---- helpers -------------------------------------------------------------------------------------------
    def recent(self, fp: str, actions: set[str], hours: float) -> bool:
        return any(e.get("fp") == fp and e.get("action") in actions and since(e["at"], hours) for e in self.events)

    def fixes_today(self) -> int:
        return sum(1 for f in self.fixes if since(f["at"], 24))

    def finish(self, record: dict, action: str, msg: str, notify: str | None, url: str = ""):
        record["action"] = action
        self.events.append(record)
        self.state.data["events"] = self.events[-200:]
        self.state.save()
        head = f"**{record.get('workflow', 'improve')}**" + (f" run [{record['run']}]({url})" if record.get("run") else "")
        self.state.log(f"{head}: `{action}`\n\n{msg}")
        if notify:
            tag = "[dry run] " if self.dry else ""
            telegram.send(f"{tag}{notify} | {self.ch['name']} ({self.repo.split('/')[-1]}) {record.get('workflow', '')}\n{msg}\n{url}".strip())
        print(f"{action}: {msg}")

    def diagnose(self, workflow: str, steps: str, log: str, wf_file: str | None, extra: str = "") -> dict:
        if not self.brain.ready:
            return {}
        ctx = fixer.context(self.target, self.lane, log, wf_file)
        return fixer.ask(self.brain, self.lane, workflow, steps, log, ctx, extra) or {}

    # ---- failed run ----------------------------------------------------------------------------------------
    def heal(self, run_id: str):
        gh, repo = self.gh, self.repo
        run = gh.get(f"/repos/{repo}/actions/runs/{run_id}")
        default = gh.get(f"/repos/{repo}")["default_branch"]
        url, wf_name = run["html_url"], run["name"]
        wf_file = run["path"].split("/")[-1].split("@")[0]
        wf_cfg = self.lane.get("workflows", {}).get(wf_file, {})
        gap = self.silent() if run["conclusion"] == "success" and wf_cfg.get("max_h") else None
        if run["conclusion"] not in ("failure", "timed_out") and not gap:
            print(f"run {run_id} concluded {run['conclusion']}; nothing to heal")
            return
        if run["head_branch"] != default:
            print(f"run {run_id} is on {run['head_branch']}, not {default}; ignored")
            return
        # A dry run only reports, so it may look at a failure again (to test the doctor on a real case).
        if not self.dry and any(e.get("run") == int(run_id) and e.get("attempt") == run["run_attempt"] for e in self.events):
            print("this attempt was already handled")
            return

        if reason := self.batch_recovery_guard(run_id, wf_file):
            rec = {"at": iso(), "run": int(run_id), "attempt": run["run_attempt"], "workflow": wf_name,
                   "kind": "batch-owned", "evidence": reason[:200], "fp": fingerprint(wf_name, "batch-owned")}
            return self.finish(rec, "batch-owned", reason, None, url)

        if gap:
            return self.heal_silent(run, gap, wf_name, wf_file, url, default)

        jobs = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs", filter="latest", per_page=50)["jobs"]
        failed = [j for j in jobs if j["conclusion"] in ("failure", "timed_out", "cancelled")]
        steps = ", ".join(f"{j['name']} / {s['name']}" for j in failed for s in j.get("steps", [])
                          if s.get("conclusion") == "failure") or "unknown step"
        raw, log = self.logs(run_id, failed)
        kind, evidence = classify(raw, run["conclusion"])
        producer = self.producer()
        if kind == "unknown" and producer and wf_file != producer and SILENT.search(evidence) and (gap := self.silent()):
            green = next((r for r in gh.get(f"/repos/{repo}/actions/workflows/{producer}/runs", per_page=10)
                          .get("workflow_runs", []) if r.get("conclusion") == "success"), None)
            if green:
                print(f"{wf_name} failed because the channel is quiet; healing {producer} run {green['id']} instead")
                return self.heal_silent(green, gap, green["name"], producer, green["html_url"], default)
        fp = fingerprint(wf_name, evidence)
        rec = {"at": iso(), "run": int(run_id), "attempt": run["run_attempt"], "workflow": wf_name, "fp": fp,
               "kind": kind, "evidence": evidence[:200]}
        print(f"{wf_name} run {run_id} attempt {run['run_attempt']}: {kind} | {evidence}")
        if kind == "unknown" and SILENT.search(evidence) and (newest := self.published_after(run)):
            return self.finish(rec, "recovered", f"{wf_name} failed on silence ({evidence}), but the channel has "
                               f"published since (\"{newest}\"); nothing to do.", None, url)

        if kind == "billing":
            notify = None if self.recent(fp, {"escalated"}, 12) else "NEEDS YOU"
            return self.finish(rec, "escalated", "GitHub billing is blocking this repo's Actions. Free fix, no "
                               "payment: the agent makes the repo public after a history scan for "
                               "secrets and personal data (ALTERNATIVES.md, GitHub Actions).", notify, url)
        if kind == "auth":
            notify = None if self.recent(fp, {"escalated"}, 12) else "NEEDS YOU"
            return self.finish(rec, "escalated", f"A credential was rejected, so a re-login or new key is needed:\n{evidence}",
                               notify, url)
        if kind == "quota":
            n = sum(1 for e in self.events if e.get("kind") == "quota" and e.get("workflow") == wf_name and since(e["at"], 24))
            notify = "WATCHING" if n >= 2 and not self.recent(fp, {"quota-wait"}, 12) else None
            return self.finish(rec, "quota-wait", f"Out of a free quota; the next scheduled run will retry.\n{evidence}",
                               notify, url)
        if kind == "transient":
            if run["run_attempt"] < 3:
                if not self.dry:
                    gh.post(f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs")
                return self.finish(rec, "rerun", f"Temporary failure ({evidence}); re-ran the failed jobs "
                                   f"(attempt {run['run_attempt'] + 1}).", "RETRYING" if run["run_attempt"] > 1 else None, url)
            kind = rec["kind"] = "unknown"

        # A doctor change that landed before this failure gets rolled back first.
        for f in self.fixes:
            if f.get("status") == "pending" and f["workflow"] in (wf_name, "*") and since(f["at"], 24):
                try:
                    cmp = gh.get(f"/repos/{repo}/compare/{f['sha']}...{run['head_sha']}")["status"]
                except urllib.error.HTTPError:
                    continue
                if cmp in ("ahead", "identical"):
                    f["status"] = "reverted"
                    if not self.dry and self.can_edit:
                        sha = fixer.revert(self.target, f["sha"], f"{wf_name} still failing after it")
                        f["reverted_by"] = sha
                    return self.finish(rec, "reverted", f"My change {f['sha'][:7]} ({f['summary']}) did not hold: "
                                       f"{wf_name} failed after it ({evidence}). I rolled it back; this needs a person.",
                                       "REVERTED", url)

        head = fixer.git(self.target, "rev-parse", "HEAD")
        since_fail = fixer.git(self.target, "log", "--format=%h %s", f"{run['head_sha']}..HEAD", "--", ".",
                               ":(exclude)state", check=False) if head != run["head_sha"] else ""
        extra = (f"- The failed run used commit {run['head_sha'][:7]}; the checkout is now at {head[:7]}. Commits since:\n"
                 f"{since_fail[:1500]}\n  If they already fix it, return no edits and say so.\n" if since_fail else "")

        log += self.code_hint(log, f"{wf_name} failed at {steps}: {evidence}")
        may_fix = self.can_edit and wf_cfg.get("fix", True)
        if not may_fix or self.recent(fp, {"fixed", "fix-failed", "fix-refused"}, 24) or self.fixes_today() >= self.lane["max_fixes_per_day"]:
            why = ("this lane is report-only" if not may_fix else
                   "I already tried this failure in the last 24 h" if self.recent(fp, {"fixed", "fix-failed", "fix-refused"}, 24)
                   else "today's fix budget is used up")
            plan = self.diagnose(wf_name, steps, log, wf_file, extra + "- Diagnose only: return no edits.\n")
            if plan.get("already_fixed"):
                return self.finish(rec, "already-fixed", f"Cause: {plan.get('diagnosis')}\nAlready fixed on the "
                                   "default branch; nothing to do.", None, url)
            msg = f"Cause: {plan.get('diagnosis') or evidence}\n" + (f"What to do: {plan['human_action']}\n" if plan.get("human_action") else "")
            notify = None if self.recent(fp, {"escalated"}, 12) else "NEEDS YOU"
            return self.finish(rec, "escalated", msg + f"(No code change: {why}.)", notify, url)

        self.attempt(rec, wf_name, wf_file, steps, log, extra, url, default)

    def batch_recovery_guard(self, run_id: str, workflow: str) -> str | None:
        """The durable media coordinator owns batch retries, holds, source repairs and upload intents.

        Retrying just a failed publish job can reuse refused media and an obsolete script. The doctor must
        neither rerun nor patch that production behind the coordinator's exact-media checks.
        """
        if workflow not in {"run.yml", "shorts.yml", "batch-release.yml"} or not (
                self.target / "strategy" / "batch-2026-10-25.json").is_file():
            return None
        try:
            row = self.gh.get(f"/repos/{self.repo}/contents/batch-production.json", ref="state")
            data = json.loads(base64.b64decode(row["content"], validate=False))
            jobs = data["jobs"]
            if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) for j in jobs):
                raise ValueError("invalid batch jobs")
            if any(not isinstance(j.get(field, []), list) for j in jobs
                   for field in ("obsolete_runs", "repair_history")):
                raise ValueError("invalid batch run history")
        except (KeyError, ValueError, TypeError, urllib.error.URLError, TimeoutError):
            return "Batch recovery state is unavailable; preserve all holds and intents for coordinator reconciliation."
        number = int(run_id)
        for job in jobs:
            owned = [job.get("run_id"), *job.get("obsolete_runs", []),
                     *(h.get("run_id") for h in job.get("repair_history", []) if isinstance(h, dict))]
            if number in owned:
                return (f"Batch entry {job.get('premise', '?')} ({job.get('stage', 'unknown')}) owns this run. "
                        "Its coordinator must reconcile exact media, holds and upload markers; no doctor rerun or code change.")
        if data.get("enabled") and any(j.get("stage") not in {"public", "scheduled"} for j in jobs):
            return "An active reviewed batch owns this producer's calendar; preserve its coordinator and reserved deliveries."
        return None

    # ---- green run, silent or defective channel -------------------------------------------------------------
    def silent(self) -> tuple[str, str] | None:
        """(kind, why) when the channel is silent ("quiet", "thin") or its recent uploads show defects ("qa")."""
        if not self.ch.get("youtube") or self.ch.get("hands_off"):
            return None
        try:
            return watch.symptom(self.ch, watch.feed(self.gh, self.ch["youtube"]), now(),
                                 watch.code_changed(self.gh, self.repo, self.lane))
        except RuntimeError:
            return None

    def published_after(self, run: dict) -> str | None:
        """Title of the channel's newest upload when it went public after `run` started, else None."""
        if not self.ch.get("youtube") or not run.get("created_at"):
            return None
        try:
            vids = watch.feed(self.gh, self.ch["youtube"])
        except RuntimeError:
            return None
        at = now()
        newest = next((v for v in vids if v.get("published")), None)
        return newest["title"] if newest and watch.hours(newest["published"], at) < watch.hours(run["created_at"], at) else None

    def producer(self) -> str | None:
        return next((wf for wf, w in self.lane.get("workflows", {}).items() if w.get("max_h")), None)

    def logs(self, run_id, jobs) -> tuple[str, str]:
        """(raw logs for the playbook, the readable excerpt for the brain) of up to two jobs."""
        raw = log = ""
        for j in jobs[:2]:
            try:
                text = str(self.gh.get(f"/repos/{self.repo}/actions/jobs/{j['id']}/logs"))
            except Exception as exc:
                text = f"(log unavailable: {type(exc).__name__})"
            raw += f"\n### job: {j['name']}\n" + text
            log += f"\n### job: {j['name']}\n" + excerpt(text, 300)
        return raw, log

    def code_hint(self, log: str, task: str) -> str:
        """A log that names no source file (a hold, a gate, silence) gets the brain's pick of up to 3 editable files,
        written as traceback lines so fixer.context shows them; otherwise the brain would see no code at all."""
        if fixer.referenced(log, self.target) or not self.brain.ready:
            return ""
        files = [p for p in fixer.git(self.target, "ls-files").splitlines() if config.editable(p, self.lane)]
        if not files:
            return ""
        pick = self.brain.ask_json(
            "Pick at most 3 files from this list that most likely hold the code behind the problem below. Return JSON "
            '{"files": ["path", ...]}.\nProblem:\n' + task[:1500] + "\nLog:\n" + log[-4000:] + "\nFiles:\n" +
            "\n".join(files[:400]), "fast") or {}
        chosen = [p for p in pick.get("files", []) if p in files][:3]
        return "".join(f'\nFile "{p}", line 1' for p in chosen)

    def heal_silent(self, run, gap, wf_name, wf_file, url, default):
        kind, why = gap
        jobs = self.gh.get(f"/repos/{self.repo}/actions/runs/{run['id']}/jobs", filter="latest", per_page=50)["jobs"]
        _, log = self.logs(run["id"], jobs)
        label = "silent" if kind in ("quiet", "thin") else kind
        fp = fingerprint(wf_name, label)
        rec = {"at": iso(), "run": int(run["id"]), "attempt": run["run_attempt"], "workflow": wf_name, "fp": fp,
               "kind": label, "evidence": why[:200]}
        print(f"{wf_name} run {run['id']} succeeded but {why}")
        if label == "silent":
            task = (f"- This run SUCCEEDED, yet the channel is silent: {why}.\n  Find in the log why it made or "
                    "published nothing (skips, holds, gates, an empty buffer, nothing eligible) and change the code so "
                    "the lane produces publishable output.")
            steps = "green run, nothing published"
        else:
            task = (f"- This run SUCCEEDED, but what the channel published has visible defects: {why}.\n  Find the code "
                    "that builds titles or decides uploads and fix the cause for every future upload (casing, "
                    "leftovers, duplicates). Already-published videos are not changed by code.")
            steps = "green run, defective uploads"
        task += (" Never weaken a safety, rights, factual or quality gate, never invent facts or fake real footage; if "
                 "the only fix is a looser gate or the owner's action, set fixable_in_code false and say what to do.\n")
        log += self.code_hint(log, task)
        tried = self.recent(fp, {"fixed", "fix-failed", "fix-refused", "escalated"}, 24)
        if not (self.can_edit and self.lane["workflows"].get(wf_file, {}).get("fix", True)) or tried \
                or self.fixes_today() >= self.lane["max_fixes_per_day"]:
            reason = (f"I already looked at this ({label}) in the last 24 h" if tried else
                      "this lane is report-only" if not self.can_edit else "today's fix budget is used up")
            plan = self.diagnose(wf_name, steps, log, wf_file, task + "- Diagnose only: return no edits.\n")
            msg = f"Cause: {plan.get('diagnosis') or why}\n" + (f"What to do: {plan['human_action']}\n" if plan.get("human_action") else "")
            return self.finish(rec, "escalated", msg + f"(No code change: {reason}.)",
                               None if tried else label.upper(), url)
        self.attempt(rec, wf_name, wf_file, steps, log, task, url, default)

    def attempt(self, rec, wf_name, wf_file, steps, log, extra, url, default, improvement=False):
        ctx = fixer.context(self.target, self.lane, log, wf_file)
        plan = fixer.ask(self.brain, self.lane, wf_name, steps, log, ctx, extra) if self.brain.ready else None
        if plan is None and self.brain.down:
            notify = None if improvement or self.recent(rec["fp"], {"brain-down", "escalated"}, 12) else "WATCHING"
            return self.finish(rec, "brain-down", f"Cause so far: {rec.get('evidence', '')}\nGemini was overloaded for every "
                               "model after three rounds, so no fix yet; the next failure of this workflow tries again.",
                               notify, url)
        if isinstance(plan, dict) and plan.get("already_fixed") and not improvement:
            return self.finish(rec, "already-fixed", f"Cause: {plan.get('diagnosis')}\nAlready fixed on the default "
                               "branch; nothing to do.", None, url)
        staged, why = fixer.validate(plan, self.lane, self.target)
        tries = 0
        while not why and tries < 2:
            tries += 1
            fixer.apply(staged, self.target)
            ok, out = fixer.run_tests(self.target, self.lane)
            if ok:
                break
            fixer.discard(self.target)
            if tries == 2:
                why = f"my patch failed the repo's tests:\n{out[-800:]}"
                break
            plan = fixer.ask(self.brain, self.lane, wf_name, steps, log, ctx,
                             extra + f"- Your previous patch failed the tests with:\n{out[-2500:]}\n  Fix that too.\n")
            staged, why = fixer.validate(plan, self.lane, self.target)
        diagnosis = (plan or {}).get("diagnosis") or rec.get("evidence", "")
        if why:
            action = "fix-refused" if "not fixable" in why or "confidence" in why else "fix-failed"
            human = (plan or {}).get("human_action")
            if improvement:
                return self.finish(rec, "improve-skipped", f"{diagnosis}\nSkipped: {why}", None)
            notify = None if self.recent(rec["fp"], {"escalated", "fix-failed", "fix-refused"}, 12) else "NEEDS YOU"
            return self.finish(rec, action, f"Cause: {diagnosis}\n" + (f"What to do: {human}\n" if human else "") +
                               f"(No code change: {why}.)", notify, url)
        files = [s["path"] for s in staged]
        summary = ((plan or {}).get("summary") or "fix failing run")[:70]
        if self.dry:
            diff = fixer.git(self.target, "diff", check=False)[:3000]
            fixer.discard(self.target)
            return self.finish(rec, "would-fix", f"Cause: {diagnosis}\nWould push ({', '.join(files)}), tests pass:\n```diff\n{diff}\n```",
                               "DRY RUN", url)
        sha = fixer.commit_push(self.target, f"doctor: {summary}" + (f" (run {rec['run']})" if rec.get("run") else ""), files)
        self.fixes.append({"at": iso(), "sha": sha, "workflow": "*" if improvement else wf_name, "fp": rec["fp"],
                           "summary": summary, "status": "pending", "model": self.brain.used})
        redispatched = ""
        if not improvement and wf_file in (self.lane.get("redispatch") or []):
            try:
                self.gh.post(f"/repos/{self.repo}/actions/workflows/{wf_file}/dispatches", {"ref": default})
                redispatched = f" Started {wf_name} again."
            except urllib.error.HTTPError as e:
                redispatched = f" Could not restart {wf_name} (HTTP {e.code}); the next scheduled run will use the fix."
        link = f"https://github.com/{self.repo}/commit/{sha}"
        self.finish(rec, "improved" if improvement else "fixed",
                    f"{'Change' if improvement else 'Cause'}: {diagnosis}\nPushed {sha[:7]} \"{summary}\" ({', '.join(files)}); "
                    f"the repo's tests pass. I roll it back automatically if the next run fails.{redispatched}\n{link}",
                    "IMPROVED" if improvement else "FIXED", url)

    # ---- twice-weekly improvement --------------------------------------------------------------------------
    def improve(self):
        if self.ch.get("paused"):
            print("channel is paused; improvement skipped")
            return
        if not self.can_edit:
            print("lane is not allowed to change code; improvement skipped")
            return
        raw = self.gh.text(REVIEW_URL)
        recs = []
        if raw:
            try:
                recs = json.loads(raw).get("channels", {}).get(self.key, {}).get("recommendations", [])
            except json.JSONDecodeError:
                pass
        recs = [r for r in recs if r.get("auto_ok") and r.get("target") == "code"]
        done = {e.get("fp") for e in self.events if e.get("action") in ("improved", "improve-skipped")}
        recs = [r for r in recs if fingerprint("improve", r.get("change", "")) not in done]
        if not recs:
            print("no safe code improvement waiting for this channel")
            return
        if self.fixes_today() >= self.lane["max_fixes_per_day"]:
            print("fix budget used up today")
            return
        r = recs[0]
        rec = {"at": iso(), "workflow": "improve", "fp": fingerprint("improve", r.get("change", "")), "kind": "improve"}
        files = [p for p in fixer.git(self.target, "ls-files").splitlines() if config.editable(p, self.lane)]
        pick = self.brain.ask_json(
            "Pick at most 3 files from this list that must change to make the improvement below. Return JSON "
            '{"files": ["path", ...]}.\nImprovement: ' + r.get("change", "") + "\nEvidence: " + r.get("evidence", "") +
            "\nFiles:\n" + "\n".join(files[:400]), "fast") or {}
        chosen = [p for p in pick.get("files", []) if p in files][:3]
        log = "\n".join(f'File "{p}", line 1' for p in chosen)
        task = (f"- This is a planned improvement from the twice-weekly review, not a failure:\n  {r.get('change')}\n"
                f"  Evidence: {r.get('evidence')}\n  Implement it only if it is small, safe, testable and consistent with "
                f"CHANNEL.md and the mission below; otherwise set fixable_in_code false.\n{mission.brief(3500)}\n")
        self.attempt(rec, "improve", None, "n/a", log, task, "", "", improvement=True)
