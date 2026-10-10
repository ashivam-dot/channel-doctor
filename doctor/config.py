from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "channels.json"

# Never edited by the doctor, whatever a lane's `editable` list says.
PROTECTED = [".github/**", "**/*.md", "tests/**", "**/test_*.py", "**/conftest.py", "secrets/**", "**/.env*",
             "**/*.lock", "pyproject.toml", "**/*.pem", "**/*.json", "state/**"]


def load(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text())


def lanes(cfg: dict):
    for key, ch in cfg["channels"].items():
        for lane in ch.get("lanes", []):
            yield key, ch, lane


def lane_for(cfg: dict, repo: str):
    name = repo.split("/")[-1]
    for key, ch, lane in lanes(cfg):
        if lane["repo"] == name:
            return key, ch, {**cfg.get("defaults", {}), **lane}
    raise SystemExit(f"{repo} is not in channels.json")


def glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def matches(path: str, patterns: list[str]) -> bool:
    return any(glob_re(p).match(path) for p in patterns)


def editable(path: str, lane: dict) -> bool:
    if ".." in Path(path).parts:
        return False
    while path.startswith(("./", "/")):
        path = path[2:] if path.startswith("./") else path[1:]
    return matches(path, lane.get("editable") or []) and not matches(path, PROTECTED)
