from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_plain = urllib.request.build_opener(_NoRedirect)


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "User-Agent": "channel-doctor",
             "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def call(self, method: str, path: str, body=None, **params):
        url = path if path.startswith("http") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        try:
            with _plain.open(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                with urllib.request.urlopen(e.headers["Location"], timeout=120) as r:
                    return r.read().decode("utf-8", "replace")
            raise
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    def get(self, path, **params):
        return self.call("GET", path, **params)

    def post(self, path, body=None):
        return self.call("POST", path, body if body is not None else {})

    def patch(self, path, body):
        return self.call("PATCH", path, body)

    def put(self, path, body):
        return self.call("PUT", path, body)

    def text(self, url: str) -> str | None:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "channel-doctor"}), timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            return None

    def put_file(self, repo: str, path: str, content: str, message: str, branch: str = "main"):
        try:
            sha = self.get(f"/repos/{repo}/contents/{urllib.parse.quote(path)}", ref=branch).get("sha")
        except urllib.error.HTTPError:
            sha = None
        body = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": branch,
                "committer": {"name": "github-actions[bot]",
                              "email": "41898282+github-actions[bot]@users.noreply.github.com"}}
        if sha:
            body["sha"] = sha
        return self.put(f"/repos/{repo}/contents/{urllib.parse.quote(path)}", body)

    def ensure_label(self, repo: str, name: str, color: str = "b60205"):
        try:
            self.get(f"/repos/{repo}/labels/{urllib.parse.quote(name)}")
        except urllib.error.HTTPError:
            try:
                self.post(f"/repos/{repo}/labels", {"name": name, "color": color})
            except urllib.error.HTTPError:
                pass
