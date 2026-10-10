"""Ask the model for a small patch, check it against the lane's guard rails, prove it with the repo's tests."""
from __future__ import annotations

import difflib
import json
import re
import subprocess
from pathlib import Path

from . import config
from .redact import redact

BOT = ["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
TRACE = re.compile(r'File "(?:/home/runner/work/[^/]+/[^/]+/|/github/workspace/)?(?:target/)?([^"]+\.py)", line (\d+)')
PYTEST = re.compile(r"^([\w./-]+\.py):(\d+)", re.M)


def git(root: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def referenced(log: str, root: Path) -> dict[str, list[int]]:
    refs: dict[str, list[int]] = {}
    for path, line in TRACE.findall(log) + PYTEST.findall(log):
        rel = path.split("/site-packages/")[-1] if "site-packages" in path else path
        if (root / rel).is_file() and ".venv" not in rel:
            refs.setdefault(rel, []).append(int(line))
    return refs


def excerpt(text: str, lines: list[int], radius: int = 120) -> str:
    rows = text.splitlines()
    if len(rows) <= 600:
        return text
    keep = set()
    for n in lines or [1]:
        keep.update(range(max(0, n - radius), min(len(rows), n + radius)))
    out, last = [], -2
    for i in sorted(keep):
        if i != last + 1:
            out.append(f"... (line {i + 1})")
        out.append(rows[i])
        last = i
    return "\n".join(out)


def context(root: Path, lane: dict, log: str, workflow_file: str | None, budget: int = 90_000) -> str:
    parts = []
    channel_md = root / "CHANNEL.md"
    if channel_md.is_file():
        parts.append("## CHANNEL.md (rules the fix must respect, first part)\n" + channel_md.read_text()[:3000])
    if workflow_file and (root / ".github/workflows" / workflow_file).is_file():
        parts.append(f"## .github/workflows/{workflow_file} (read only)\n" + (root / ".github/workflows" / workflow_file).read_text()[:6000])
    parts.append("## Recent commits\n" + git(root, "log", "-8", "--stat", "--format=%h %ad %s", "--date=iso", check=False)[:5000])
    refs = referenced(log, root)
    for path, lines in list(refs.items())[:6]:
        body = excerpt((root / path).read_text(errors="replace"), lines)
        flag = "EDITABLE" if config.editable(path, lane) else "read only"
        parts.append(f"## {path} ({flag})\n{body}")
    for path in refs:
        recent = git(root, "log", "-2", "-p", "--format=%h %s", "--", path, check=False)
        if recent:
            parts.append(f"## Recent changes to {path}\n{recent[:5000]}")
    text = "\n\n".join(parts)
    return redact(text[:budget])


PROMPT = """You are the on-call engineer for an automated YouTube channel pipeline that runs on GitHub Actions.
A workflow run failed. Decide whether a small, safe code change fixes the cause, and if so write it.
The channel's goal is to reach the YouTube Partner Program soon: every missed publish is lost reach, and every
quality, originality or policy check exists to keep the channel monetisable, so never trade one for the other.

Rules:
- Only edit files listed as EDITABLE; the editable globs are {editable}. Never edit workflows, tests, docs, lock files or secrets.
- Make the smallest change that fixes the root cause. Never weaken a safety or quality gate (fact checks, duplicate checks,
  privacy checks, licence checks, tests) to make the run pass. Never hard-code around one bad input if a general fix exists.
- If the cause is external (outage, quota, billing, revoked credentials, a missing secret, a site change you cannot see),
  or you are not confident, set fixable_in_code to false and explain what a human must do.
- Each edit replaces one exact, unique snippet: `find` must appear exactly once in the file, copied verbatim with its indentation.
- If commits since the failed run already fix the cause, set already_fixed to true and return no edits.
- Respect the channel rules in CHANNEL.md.
{extra}
Return JSON only:
{{"diagnosis": "one or two plain sentences on the root cause",
  "category": "code|data|config|external|unknown",
  "fixable_in_code": true,
  "already_fixed": false,
  "confidence": 0.0,
  "edits": [{{"path": "relative/path.py", "find": "exact old text", "replace": "new text"}}],
  "summary": "imperative commit subject, under 70 characters",
  "human_action": "what the owner must do if not fixable, else empty"}}

Workflow: {workflow}   Failed step(s): {steps}
## Failure log (tail, credentials redacted)
{log}

{context}
"""


def ask(brain, lane: dict, workflow: str, steps: str, log: str, ctx: str, extra: str = ""):
    prompt = PROMPT.format(editable=json.dumps(lane.get("editable") or []), workflow=workflow, steps=steps,
                           log=redact(log)[-12000:], context=ctx, extra=extra)
    return brain.ask_json(prompt, "deep")


def validate(plan: dict, lane: dict, root: Path) -> tuple[list[dict], str | None]:
    if not isinstance(plan, dict):
        return [], "no usable answer from the model"
    if not plan.get("fixable_in_code") or not plan.get("edits"):
        return [], "model judged it not fixable in code"
    if float(plan.get("confidence") or 0) < 0.6:
        return [], f"model confidence too low ({plan.get('confidence')})"
    edits = plan["edits"]
    if len(edits) > 8 or len({e.get("path") for e in edits}) > lane.get("max_files", 4):
        return [], "patch touches too many files"
    changed = 0
    staged: dict[str, str] = {}
    for e in edits:
        path, find, repl = (e.get("path") or "").strip(), e.get("find") or "", e.get("replace")
        if repl is None or not find:
            return [], f"edit for {path} is incomplete"
        if not config.editable(path, lane):
            return [], f"{path} is outside the editable paths"
        f = root / path
        if not f.is_file():
            return [], f"{path} does not exist"
        text = staged.get(path, f.read_text())
        if text.count(find) != 1:
            return [], f"snippet in {path} found {text.count(find)} times, need exactly 1"
        new = text.replace(find, repl)
        changed += sum(1 for l in difflib.unified_diff(text.splitlines(), new.splitlines(), lineterm="")
                       if l[:1] in "+-" and not l.startswith(("+++", "---")))
        staged[path] = new
    if changed > lane.get("max_changed_lines", 80):
        return [], f"patch changes {changed} lines, limit {lane.get('max_changed_lines', 80)}"
    return [{"path": p, "text": t} for p, t in staged.items()], None


def apply(staged: list[dict], root: Path):
    for s in staged:
        (root / s["path"]).write_text(s["text"])


def run_tests(root: Path, lane: dict) -> tuple[bool, str]:
    if lane.get("apt"):
        subprocess.run("sudo apt-get update -qq && sudo apt-get install -y -qq " + " ".join(lane["apt"]),
                       shell=True, cwd=root, capture_output=True)
    cmd = lane.get("test")
    if not cmd:
        return False, "lane has no test command"
    for s in [p for p in git(root, "diff", "--name-only").splitlines() if p.endswith(".py")]:
        r = subprocess.run(["python3", "-m", "py_compile", s], cwd=root, capture_output=True, text=True)
        if r.returncode:
            return False, r.stderr[-2000:]
    try:
        r = subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True, timeout=lane.get("test_timeout_s", 1500))
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    out = (r.stdout + r.stderr)[-3000:]
    return r.returncode == 0, out


def discard(root: Path):
    git(root, "checkout", "--", ".", check=False)


def commit_push(root: Path, message: str, paths: list[str]) -> str:
    git(root, "add", "--", *paths)
    git(root, *BOT, "commit", "-m", message, "--", *paths)
    discard(root)
    return push(root)


def revert(root: Path, sha: str, reason: str) -> str:
    git(root, *BOT, "pull", "--rebase", "origin", git(root, "rev-parse", "--abbrev-ref", "HEAD"), check=False)
    git(root, *BOT, "revert", "--no-edit", sha)
    git(root, *BOT, "commit", "--amend", "-m", f"doctor: revert {sha[:7]} ({reason})")
    return push(root)


def push(root: Path) -> str:
    for _ in range(3):
        if subprocess.run(["git", "push", "origin", "HEAD"], cwd=root, capture_output=True).returncode == 0:
            return git(root, "rev-parse", "HEAD")
        git(root, *BOT, "pull", "--rebase", "origin", git(root, "rev-parse", "--abbrev-ref", "HEAD"), check=False)
    raise RuntimeError("push failed three times")
