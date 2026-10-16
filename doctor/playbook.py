"""Known failure shapes and what to do about them, before any AI is involved."""
from __future__ import annotations

import hashlib
import re

RULES = [
    ("billing", r"recent account payments have failed|spending limit needs to be increased|account is locked due to a billing issue"),
    ("auth", r"invalid_grant|Token has been expired or revoked|unauthorized_client|Bad credentials|"
             r"API key not valid|API_KEY_INVALID|PERMISSION_DENIED.*(?:key|credential)|youtubeSignupRequired"),
    ("quota", r"quotaExceeded|uploadLimitExceeded|RESOURCE_EXHAUSTED|dailyLimitExceeded|rateLimitExceeded|"
              r"exceeded your current quota|429 Too Many Requests"),
    ("transient", r"ECONNRESET|Connection reset by peer|Temporary failure in name resolution|Could not resolve host|"
                  r"HTTP Error 50[234]|50[234] (?:Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out)|"
                  r"Read timed out|RemoteDisconnected|IncompleteRead|The runner has received a shutdown signal|"
                  r"lost communication with the server|The operation was canceled|Connection aborted|"
                  r"Failed to download action|Unable to download artifact|Error: Process completed with exit code 143|"
                  r"No space left on device|ServiceUnavailable|backendError|ProtocolError"),
]
ERRORISH = re.compile(r"Traceback|Error|Exception|error:|FAILED|::error|fatal:|exit code [1-9]")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean(log: str) -> str:
    return "\n".join(ANSI.sub("", TIMESTAMP.sub("", line)) for line in log.splitlines())


# Runner housekeeping that follows every job. Read as evidence it made all 19 failures in the doctor logs "unknown"
# ("Cleaning up orphan processes", the Node.js 20 notice) and hid the real error printed above it.
NOISE = re.compile(r"^(?:Post job cleanup\.?|Cleaning up orphan processes|\[command\]/usr/bin/git\b|Temporarily overriding HOME|"
                   r"Adding repository directory|http\.https://github\.com/\.extraheader|##\[warning\]Node\.js 20 is deprecated|"
                   r"(?:UV_CACHE_DIR|UV_PYTHON_INSTALL_DIR) is already set|Cache (?:hit occurred|saved|restored|not found)|"
                   r"Copying '/home/runner/|Entering '|git version \d|Evaluate and set environment url|Uploaded bytes \d)")
STEP_ERROR = re.compile(r"^##\[error\]")


def without_env(lines: list[str]) -> list[str]:
    """Each step's command line kept, the shell/env listing GitHub prints under it dropped."""
    out, skipping = [], False
    for line in lines:
        if line.startswith("##[group]"):
            skipping = True
            if line.startswith("##[group]Run "):
                out.append(line.replace("##[group]", "", 1))
        elif line == "##[endgroup]":
            skipping = False
        elif not skipping:
            out.append(line)
    return out


def tail(log: str, n: int = 250) -> str:
    lines = [l for l in without_env(clean(log).splitlines()) if l.strip() and not NOISE.search(l)]
    return "\n".join(lines[-n:])


def failure_region(log: str, n: int = 150) -> str:
    """What each failing step printed (its command line, then its output up to its ##[error] line), without the
    step's env listing or runner housekeeping; "" when the log marks no step error."""
    lines = clean(log).splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not STEP_ERROR.match(line):
            continue
        start = next((j for j in range(i - 1, -1, -1) if lines[j].startswith("##[group]Run ")), max(0, i - n))
        seg = lines[start:i + 1]
        if seg[0].startswith("##[group]Run "):
            end = next((k for k, s in enumerate(seg) if s == "##[endgroup]"), 0)
            seg = [seg[0].replace("##[group]", "", 1)] + seg[end + 1:]
        out += [s for s in seg if s.strip() and not NOISE.search(s) and s != "##[endgroup]"][-n:]
    return "\n".join(out[-2 * n:])


def excerpt(log: str, n: int = 300) -> str:
    """The part of a job log worth reading: the failing steps' output, else the log's tail."""
    region = failure_region(log, n // 2)
    return region + "\n--- end of the job ---\n" + tail(log, 25) if region else tail(log, n)


def classify(log: str, conclusion: str = "failure") -> tuple[str, str]:
    """Return (kind, evidence line). Kinds: billing, auth, quota, transient, unknown."""
    region = failure_region(log) or tail(log, 160)
    for kind, pattern in RULES:
        hits = [l for l in region.splitlines() if re.search(pattern, l, re.I)]
        if hits:
            return kind, hits[-1].strip()[:300]
    if conclusion in ("timed_out", "cancelled"):
        return "transient", f"job {conclusion}"
    return "unknown", first_error(log)


def first_error(log: str) -> str:
    lines = (failure_region(log) or tail(log, 200)).splitlines()
    told = [l for l in lines if not STEP_ERROR.match(l) and re.search(r"[A-Za-z]{3}", l)]
    picked = ([l for l in told if ERRORISH.search(l) and "exit code" not in l]
              or [l for l in told if REASON.search(l)] or [l for l in told if TELLING.search(l)])
    return (picked[-1] if picked else told[-1] if told else lines[-1] if lines else "").strip()[:300]


REASON = re.compile(r"[\"']?\b(?:reason|error|why|held|problems?|refusals?)[\"']?\s*[:=]", re.I)
TELLING = re.compile(r"\b(?:fail\w*|refus\w*|held|hold|reason|unavailable|missing|denied|invalid|not found|cannot|"
                     r"unable|needs?_?attention|no \w+ (?:for|in|passed))\b", re.I)


def fingerprint(workflow: str, evidence: str) -> str:
    norm = re.sub(r"0x[0-9a-f]+|[0-9a-f]{7,}|\d+", "#", evidence.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(f"{workflow}|{norm}".encode()).hexdigest()[:10]
