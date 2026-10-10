"""State kept as a JSON block in the body of one issue, so no branch or extra token is needed."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from .github import GitHub
from .redact import redact

BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds")


def since(stamp: str, hours: float, at: datetime | None = None) -> bool:
    return datetime.fromisoformat(stamp) >= (at or now()) - timedelta(hours=hours)


class IssueState:
    def __init__(self, gh: GitHub, repo: str, title: str, label: str, intro: str, dry: bool = False):
        self.gh, self.repo, self.title, self.label, self.intro, self.dry = gh, repo, title, label, intro, dry
        self.number = None
        self.data: dict = {}
        found = gh.get(f"/repos/{repo}/issues", state="open", labels=label, per_page=50) or []
        issue = next((i for i in found if i["title"] == title and "pull_request" not in i), None)
        if issue:
            self.number = issue["number"]
            m = BLOCK.search(issue.get("body") or "")
            self.data = json.loads(m.group(1)) if m else {}

    def _body(self) -> str:
        return f"{self.intro}\n\n<details><summary>doctor state</summary>\n\n```json\n{json.dumps(self.data, indent=1)}\n```\n</details>\n"

    def save(self):
        if self.dry:
            return
        if self.number:
            self.gh.patch(f"/repos/{self.repo}/issues/{self.number}", {"body": self._body()})
        else:
            self.gh.ensure_label(self.repo, self.label, "0e8a16")
            self.number = self.gh.post(f"/repos/{self.repo}/issues",
                                       {"title": self.title, "body": self._body(), "labels": [self.label]})["number"]

    def log(self, text: str):
        print(text)
        if self.dry:
            return
        if not self.number:
            self.save()
        self.gh.post(f"/repos/{self.repo}/issues/{self.number}/comments", {"body": redact(text)[:60000]})
