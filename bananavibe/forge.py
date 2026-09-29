"""GitHub and Forgejo REST calls. The forge token never leaves this process
and its Git child processes; the coding sandbox never sees it."""

import base64
from urllib.parse import quote

from . import httpclient

WRITE_PERMISSIONS = {"write", "maintain", "admin", "owner"}


class ForgeError(RuntimeError):
    def __init__(self, status, what):
        self.status = status
        super().__init__(f"The forge refused to {what} (HTTP {status}). Check the bot's repository access.")


class Conflict(ForgeError):
    """The resource changed or already exists (optimistic write lost)."""


class Forge:
    def __init__(self, config, token, *, transport=httpclient.request):
        if not token:
            raise ValueError("BANANAVIBE_TOKEN is not set. Add the bot token as an Actions secret.")
        self.config, self.token, self._transport = config, token, transport
        self.github = config.forge == "github"
        self.headers = {"Authorization": ("Bearer " if self.github else "token ") + token}
        if self.github:
            self.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self.identity = self._identify()

    def _identify(self):
        try:
            return self.call("GET", "/user", what="read the bot identity")
        except ForgeError as error:
            # GitHub App installation tokens have no user profile but can
            # still list their repositories; confirm that before trusting it.
            if not self.github or error.status != 403:
                raise
            data = self.call("GET", "/installation/repositories", params={"per_page": 1},
                             what="read the installation")
            if not isinstance(data, dict) or not isinstance(data.get("repositories"), list):
                raise ValueError("Unexpected GitHub installation response.") from error
            return {"login": "x-access-token", "type": "Bot"}

    @property
    def login(self):
        return str(self.identity.get("login") or "")

    def call(self, method, path, *, body=None, params=None, missing=False, what="complete a request",
             conflict=(409,)):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Invalid API path.")
        url = self.config.api_url + path
        if params:
            url += "?" + "&".join(f"{quote(str(k))}={quote(str(v), safe='')}" for k, v in params.items())
        try:
            response = self._transport(method, url, headers=self.headers, body=body, timeout=45,
                                       retries=2 if method == "GET" else 0)
        except httpclient.HTTPError as error:
            if error.status == 404 and missing:
                return None
            raise (Conflict if error.status in conflict else ForgeError)(error.status, what) from None
        except httpclient.Unreachable as error:
            raise RuntimeError(f"The forge API could not be reached: {error}") from None
        return response.json()

    def pages(self, path, params=None, *, maximum=50, what="list items"):
        items = []
        for page in range(1, maximum + 1):
            data = self.call("GET", path, params={**(params or {}), "page": page,
                                                  ("per_page" if self.github else "limit"): 50}, what=what)
            if not isinstance(data, list):
                raise RuntimeError("Unexpected forge list response.")
            items.extend(data)
            if len(data) < 50:
                return items
        raise RuntimeError(f"More than {maximum * 50} results; archive old items first.")

    # Repositories and permissions

    def repo(self, name):
        return self.call("GET", f"/repos/{name}", what=f"read {name}")

    def permission(self, repository, login):
        if not login or "/" in login:
            return "none"
        data = self.call("GET", f"/repos/{repository}/collaborators/{quote(login, safe='')}/permission",
                         missing=True, what="read collaborator permissions")
        return str((data or {}).get("permission") or "none")

    def authorized(self, login):
        """Commands need write access to both the control and the target repository."""
        repositories = {self.config.control_repository, self.config.target_repository}
        return all(self.permission(name, login) in WRITE_PERMISSIONS for name in repositories)

    # Issues and comments (always in the control repository)

    def _issues(self, suffix=""):
        return f"/repos/{self.config.control_repository}/issues{suffix}"

    def issue(self, number):
        return self.call("GET", self._issues(f"/{int(number)}"), what="read the issue")

    def comments(self, number):
        return self.pages(self._issues(f"/{int(number)}/comments"), what="read issue comments")

    def comment(self, number, text):
        data = self.call("POST", self._issues(f"/{int(number)}/comments"), body={"body": text[:60000]},
                         what="comment on the issue")
        return (data or {}).get("id")

    def edit_comment(self, comment_id, text):
        return self.call("PATCH", self._issues(f"/comments/{int(comment_id)}"), body={"body": text[:60000]},
                         what="update a status comment")

    def set_issue_state(self, number, state):
        return self.call("PATCH", self._issues(f"/{int(number)}"), body={"state": state},
                         what=f"mark the issue {state}")

    # Branches and state files

    def branch_sha(self, repository, name):
        if self.github:
            data = self.call("GET", f"/repos/{repository}/git/ref/heads/{quote(name, safe='/')}", missing=True,
                             what="read a branch")
            return data["object"]["sha"] if isinstance(data, dict) and "object" in data else None
        data = self.call("GET", f"/repos/{repository}/branches/{quote(name, safe='')}", missing=True,
                         what="read a branch")
        return data["commit"]["id"] if data else None

    def create_branch(self, repository, name, sha):
        if self.github:
            return self.call("POST", f"/repos/{repository}/git/refs", body={"ref": "refs/heads/" + name, "sha": sha},
                             what="create a branch", conflict=(409, 422))
        return self.call("POST", f"/repos/{repository}/branches", body={"new_branch_name": name, "old_ref_name": sha},
                         what="create a branch", conflict=(409, 422))

    def content(self, path):
        return self.call("GET", f"/repos/{self.config.control_repository}/contents/{quote(path, safe='/')}",
                         params={"ref": self.config.state_branch}, missing=True, what="read task state")

    def put_content(self, path, raw, previous_sha=None, message="Record BananaVibe task state"):
        body = {"message": message, "branch": self.config.state_branch, "content": base64.b64encode(raw).decode()}
        if previous_sha:
            body["sha"] = previous_sha
        # Forgejo creates files with POST and updates them with PUT.
        method = "PUT" if self.github or previous_sha else "POST"
        return self.call(method, f"/repos/{self.config.control_repository}/contents/{quote(path, safe='/')}",
                         body=body, what="save task state", conflict=(409, 422))

    # Pull requests (always in the target repository)

    def find_pull(self, branch):
        repository = self.config.target_repository
        if self.github:
            owner = repository.split("/")[0]
            pulls = self.call("GET", f"/repos/{repository}/pulls",
                              params={"state": "all", "head": f"{owner}:{branch}", "per_page": 50},
                              what="list pull requests")
        else:
            pull = self.call("GET", f"/repos/{repository}/pulls/{quote(self.config.base_branch, safe='')}/"
                             f"{quote(branch, safe='/')}", missing=True, what="find the pull request")
            if pull:
                return pull
            pulls = self.pages(f"/repos/{repository}/pulls", {"state": "all", "sort": "recentupdate"},
                               maximum=10, what="list pull requests")
        matches = [pull for pull in pulls or [] if (pull.get("head") or {}).get("ref") == branch]
        return max(matches, key=lambda pull: pull.get("number", 0)) if matches else None

    @staticmethod
    def pull_open(pull):
        return pull.get("state") == "open" and not pull.get("merged")

    def publish_pull(self, branch, title, body):
        """Create a draft PR for the branch, or refresh the open one."""
        repository = self.config.target_repository
        title, body = title[:180], body[:60000]
        existing = self.find_pull(branch)
        if existing:
            if not self.pull_open(existing):
                raise ValueError("The task's pull request was closed or merged. "
                                 "Use `/banana restart` to prepare a new contribution.")
            if not self.github:
                title = "WIP: " + title
            return self.call("PATCH", f"/repos/{repository}/pulls/{int(existing['number'])}",
                             body={"title": title, "body": body}, what="update the pull request")
        if not self.github:
            return self.call("POST", f"/repos/{repository}/pulls",
                             body={"title": "WIP: " + title, "body": body, "head": branch,
                                   "base": self.config.base_branch}, what="open a pull request")
        payload = {"title": title, "body": body, "head": branch, "base": self.config.base_branch, "draft": True}
        try:
            return self.call("POST", f"/repos/{repository}/pulls", body=payload, what="open a draft pull request",
                             conflict=())
        except ForgeError as error:
            # Draft PRs are unavailable for private repositories on some plans.
            if error.status != 422:
                raise
            payload.update(draft=False, title="[Draft] " + title)
            return self.call("POST", f"/repos/{repository}/pulls", body=payload, what="open a pull request")

    def clone_url(self, repository):
        return f"{self.config.server_url}/{repository}.git"
