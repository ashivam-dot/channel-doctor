"""The doctor's north star, loaded into every model prompt."""
from __future__ import annotations

import re

from .config import ROOT

MISSION = (ROOT / "MISSION.md").read_text()
ALTERNATIVES = (ROOT / "ALTERNATIVES.md").read_text()


def brief(limit: int = 6000) -> str:
    return "## Mission (applies to every decision)\n" + MISSION[:limit]


def alternatives(channel: str | None = None) -> str:
    """The fallback rules: the intro, the general section and one channel's (every channel's when none is named)."""
    intro, *parts = re.split(r"^## ", ALTERNATIVES, flags=re.M)
    sections = {p.split("\n", 1)[0].strip(): "### " + p.strip() for p in parts}
    keys = ["general"] + ([channel] if channel else [k for k in sections if k != "general"])
    return ("## Alternatives (fallbacks that keep quality)\n" + intro.split("\n", 1)[-1].strip() + "\n\n" +
            "\n\n".join(sections[k] for k in keys if k in sections))
