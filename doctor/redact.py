"""Strip anything that looks like a credential before text leaves the runner (Gemini prompts, Telegram, issues)."""
from __future__ import annotations

import re

PATTERNS = [
    re.compile(r"ya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"AQ\.[0-9A-Za-z_.-]{30,}"),
    re.compile(r"gh[pousr]_[0-9A-Za-z]{30,}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{30,}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}"),
    re.compile(r"sk-[0-9A-Za-z_-]{20,}"),
    re.compile(r"1//[0-9A-Za-z_-]{30,}"),
    re.compile(r"(?i)(\"?(?:refresh_token|access_token|client_secret|api[_-]?key|token|password|secret)\"?\s*[:=]\s*)\"?[^\s\",]{8,}\"?"),
    re.compile(r"(?i)(cloudinary://)[^\s]+"),
    re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"[A-Za-z0-9+/]{60,}={0,2}"),
]


def redact(text: str) -> str:
    for p in PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() and m.group(1) else "") + "[redacted]", text)
    return text
