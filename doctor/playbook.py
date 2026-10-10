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


def tail(log: str, n: int = 250) -> str:
    lines = [l for l in clean(log).splitlines() if l.strip() and not l.startswith("##[group]") and l != "##[endgroup]"]
    return "\n".join(lines[-n:])


def classify(log: str, conclusion: str = "failure") -> tuple[str, str]:
    """Return (kind, evidence line). Kinds: billing, auth, quota, transient, unknown."""
    region = tail(log, 160)
    for kind, pattern in RULES:
        hits = [l for l in region.splitlines() if re.search(pattern, l, re.I)]
        if hits:
            return kind, hits[-1].strip()[:300]
    if conclusion in ("timed_out", "cancelled"):
        return "transient", f"job {conclusion}"
    return "unknown", first_error(log)


def first_error(log: str) -> str:
    lines = tail(log, 200).splitlines()
    picked = [l for l in lines if ERRORISH.search(l) and "exit code" not in l]
    return (picked[-1] if picked else (lines[-1] if lines else "")).strip()[:300]


def fingerprint(workflow: str, evidence: str) -> str:
    norm = re.sub(r"0x[0-9a-f]+|[0-9a-f]{7,}|\d+", "#", evidence.lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(f"{workflow}|{norm}".encode()).hexdigest()[:10]
