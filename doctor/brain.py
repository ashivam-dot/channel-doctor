"""Free Gemini models, discovered at run time so model renames never break the doctor."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import local

BASE = "https://generativelanguage.googleapis.com/v1beta"
NAME = re.compile(r"^models/gemini-(\d+(?:\.\d+)?)-(pro|flash|flash-lite)(-latest|-preview(?:-\d[\d-]*)?|-\d{3})?$")
# A newer generation usually beats a bigger model of an older one, so the tier is only a bonus on the version.
BONUS = {"deep": {"pro": 0.5, "flash": 0.0, "flash-lite": -0.6},
         "fast": {"flash": 0.5, "flash-lite": 0.3, "pro": -0.3}}


def rank(names: list[str], tier: str = "deep") -> list[str]:
    scored = []
    for n in names:
        m = NAME.match(n)
        if not m:
            continue
        version, kind, suffix = float(m.group(1)), m.group(2), m.group(3) or ""
        preview = 0.2 if suffix.startswith("-preview") else 0.0
        scored.append((-(version + BONUS[tier][kind] - preview), n))
    return [n for _, n in sorted(scored)]


def _json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?|```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None


class Brain:
    def __init__(self, key: str | None = None):
        self.key = key if key is not None else (os.environ.get("DOCTOR_GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY"))
        self._models: list[str] | None = None
        self.used: str | None = None
        self.down = False
        self.local_failed = False
        self.dead: set[str] = set()

    @property
    def ready(self) -> bool:
        return bool(self.key) or local.enabled()

    def _req(self, url: str, body=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def models(self) -> list[str]:
        if self._models is None:
            names, token = [], ""
            while True:
                res = self._req(f"{BASE}/models?pageSize=200" + (f"&pageToken={token}" if token else ""))
                names += [m["name"] for m in res.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
                token = res.get("nextPageToken")
                if not token:
                    break
            self._models = names
        return self._models

    def ask_json(self, prompt: str, tier: str = "deep", models: int = 10, rounds: int = 3, pause: float = 40):
        """Gemini first; when no Gemini model answers, the local Qwen model where the job allows it."""
        out = self._gemini(prompt, tier, models, rounds, pause) if self.key else None
        if out is None and local.enabled() and not self.local_failed:
            print("no Gemini answer; asking the local Qwen model")
            try:
                out = local.ask_json(prompt)
            except Exception as exc:
                print(f"local model failed: {type(exc).__name__}: {str(exc)[:200]}")
                out = None
            if out is None:
                self.local_failed = self.down = True
            else:
                self.used, self.down = local.NAME, False
        elif out is None:
            self.down = True
        return out

    def _gemini(self, prompt: str, tier: str, models: int, rounds: int, pause: float):
        """Walk the ranked models; overloaded (503) or rate-limited (429) ones are retried in later rounds."""
        self.down = False
        try:
            ranked = rank(self.models(), tier)[:models]
        except Exception as exc:
            print(f"model list failed: {type(exc).__name__}")
            ranked = ["models/gemini-2.5-flash", "models/gemini-2.5-flash-lite"]
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
        if self.used and f"models/{self.used}" in ranked:
            ranked.remove(f"models/{self.used}")
            ranked.insert(0, f"models/{self.used}")
        busy = [m for m in ranked if m not in self.dead]
        for n in range(rounds):
            if n:
                time.sleep(pause * n)
            retry = []
            for model in busy:
                try:
                    res = self._req(f"{BASE}/{model}:generateContent", body)
                    parts = res["candidates"][0]["content"]["parts"]
                    out = _json("".join(p.get("text", "") for p in parts if not p.get("thought")))
                    if out is not None:
                        self.used = model.split("/")[-1]
                        return out
                except urllib.error.HTTPError as e:
                    print(f"{model}: HTTP {e.code}")
                    if e.code in (429, 500, 503, 504):
                        retry.append(model)
                    elif e.code in (400, 403, 404):
                        self.dead.add(model)
                except Exception as exc:
                    print(f"{model}: {type(exc).__name__}")
                    retry.append(model)
            if not retry:
                break
            busy = retry
        self.down = True
        return None
