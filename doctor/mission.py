"""The doctor's north star, loaded into every model prompt."""
from __future__ import annotations

from .config import ROOT

MISSION = (ROOT / "MISSION.md").read_text()


def brief(limit: int = 6000) -> str:
    return "## Mission (applies to every decision)\n" + MISSION[:limit]
