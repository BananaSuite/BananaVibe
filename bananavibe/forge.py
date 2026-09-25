"""Small GitHub / Forgejo API adapter. No repository token reaches the model."""

import base64
import json
import time
from urllib.parse import quote

import requests


class ForgeError(RuntimeError):
    def __init__(self, status, operation):
        self.status = status
        super().__init__(f"Forge API {operation} returned HTTP {status}. Check repository access and retry.")


class Conflict(ForgeError):
    pass


class Forge:
    def __init__(self, config, token):
        if not token:
            raise ValueError("BANANAVIBE_TOKEN is required.")
        self.config = config
        self.token = token
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers.update({"Authorization": ("Bearer " if config.forge == "github" else "token ") + token,
                                  "Accept": "application/vnd.github+json" if config.forge == "github" else "application/json",
                                  "User-Agent": "BananaVibe/2"})
        try:
            self.identity = self.request("GET", "/user")
        except ForgeError as error:
            # GitHub installation tokens can access repositories but have no
            # user profile. Validate that token type before using its Git login.
            if config.forge != "github" or error.status != 403:
                raise
            installation = self.request("GET", "/installation/repositories", params={"per_page": 1})
            if not isinstance(installation, dict) or not isinstance(installation.get("repositories"), list):
                raise ValueError("Unexpected GitHub installation response.") from error
            self.identity = {"login": "x-access-token", "type": "Bot"}

    def request(self, method, path, *, body=None, params=None, missing=False):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid API path.")
        for attempt in range(3):
            try:
                with self.http.request(method, self.config.api_url + path, json=body, params=params,
                                       timeout=(10, 45), stream=True, allow_redirects=False) as response:
                    status = response.status_code
                    if status == 404 and missing:
                        return None
                    if status in {409, 422}:
                        raise Conflict(status, method)
                    if status >= 300:
                        if method == "GET" and status in {429, 502, 503, 504} and attempt < 2:
                            time.sleep(2 ** attempt)
                            continue
                        raise ForgeError(status, method)
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            raise RuntimeError("Forge response exceeded the limit.")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    return json.loads(raw) if raw else None
            except requests.RequestException:
                if method == "GET" and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("The forge could not be reached; credentials and response bodies were withheld.") from None

    def repo(self, name):
        return self.request("GET", "/repos/" + name)

    def maintainer(self, repo, login):
        if not login or "/" in login:
            return False
        value = self.request("GET", f"/repos/{repo}/collaborators/{quote(login, safe='')}/permission", missing=True)
        return bool(value and value.get("permission") in {"write", "maintain", "admin", "owner"})

    def authorized(self, login):
        return all(self.maintainer(repo, login) for repo in {self.config.control_repository, self.config.target_repository})

    def issue(self, number):
        return self.request("GET", f"/repos/{self.config.control_repository}/issues/{int(number)}")

    def comments(self, number):
        return self.pages(f"/repos/{self.config.control_repository}/issues/{int(number)}/comments")

    def pages(self, path, params=None):
        output = []
        for page in range(1, 51):
            data = self.request("GET", path, params={**(params or {}), "page": page, "per_page": 100, "limit": 100})
            if not isinstance(data, list):
                raise RuntimeError("Unexpected forge list response.")
            output.extend(data)
            if len(data) < 100:
                return output
        raise RuntimeError("Forge result exceeds 5,000 entries. Archive old tasks or shorten the issue thread.")

    def comment(self, number, text):
        return self.request("POST", f"/repos/{self.config.control_repository}/issues/{int(number)}/comments", body={"body": text[:60000]})

    def close_issue(self, number):
        return self.request("PATCH", f"/repos/{self.config.control_repository}/issues/{int(number)}", body={"state": "closed"})

    def reopen_issue(self, number):
        return self.request("PATCH", f"/repos/{self.config.control_repository}/issues/{int(number)}", body={"state": "open"})

    def branch_sha(self, repo, name):
        if self.config.forge == "github":
            data = self.request("GET", f"/repos/{repo}/git/ref/heads/{quote(name, safe='')}", missing=True)
            return data["object"]["sha"] if data else None
        data = self.request("GET", f"/repos/{repo}/branches/{quote(name, safe='')}", missing=True)
        return data["commit"]["id"] if data else None

    def create_branch(self, repo, name, sha, *, source_branch=None):
        if self.config.forge == "github":
            return self.request("POST", f"/repos/{repo}/git/refs", body={"ref": "refs/heads/" + name, "sha": sha})
        return self.request("POST", f"/repos/{repo}/branches", body={"new_branch_name": name, "old_ref_name": sha})

    def content(self, path):
        return self.request("GET", f"/repos/{self.config.control_repository}/contents/{quote(path, safe='/')}",
                            params={"ref": self.config.state_branch}, missing=True)

    def put_content(self, path, content, previous_sha=None):
        data = {"message": "Record BananaVibe task state", "branch": self.config.state_branch,
                "content": base64.b64encode(content).decode()}
        if previous_sha:
            data["sha"] = previous_sha
        method = "PUT" if self.config.forge == "github" or previous_sha else "POST"
        return self.request(method, f"/repos/{self.config.control_repository}/contents/{quote(path, safe='/')}", body=data)

    def pull_request(self, branch, title, body):
        repo = self.config.target_repository
        owner = repo.split("/")[0]
        existing = self.pages(f"/repos/{repo}/pulls", {"state": "all", "head": f"{owner}:{branch}"})
        for pull in existing:
            if pull.get("head", {}).get("ref") == branch:
                if pull.get("state") == "closed":
                    raise ValueError("The previous PR was closed or merged. Review its outcome; use /banana restart for a new contribution.")
                # A resumed task may have changed the branch after a failed
                # publication attempt. Keep its review text and checked SHA current.
                updated = {"title": ("WIP: " if self.config.forge == "forgejo" else "") + title[:180], "body": body[:50000]}
                return self.request("PATCH", f"/repos/{repo}/pulls/{int(pull['number'])}", body=updated)
        payload = {"title": title[:180], "body": body[:50000], "head": branch, "base": self.config.base_branch, "draft": True}
        if self.config.forge == "forgejo":
            payload["title"] = "WIP: " + payload["title"]
            payload.pop("draft")
        return self.request("POST", f"/repos/{repo}/pulls", body=payload)

    def clone_url(self):
        return f"{self.config.server_url}/{self.config.target_repository}.git"
