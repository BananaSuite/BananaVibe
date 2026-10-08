"""Moving a run to another machine: `bananavibe export` and `bananavibe import`.

An export is one .tar.gz with:
- `manifest.json`: where the run came from and which repositories it manages;
- `bananavibe/`: the run directory (mission, plan, notes, journal, state, reports, and optionally logs);
- `repos/<n>.bundle`: a `git bundle` of every repository (all branches), so the work branch with every
  snapshot travels without needing a shared remote.

Import recreates the repositories (or updates existing clones), restores `.bananavibe/`, and rewrites the
state so `bananavibe start` continues exactly where the run stopped, including an interrupted session.
Uncommitted and git-ignored files (build output, node_modules, local databases) do not travel.
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tarfile
import tempfile
from pathlib import Path

from bananavibe import __version__, gitops
from bananavibe.config import Config
from bananavibe.state import DIRNAME, Paths, State
from bananavibe.util import iso, now

FORMAT = 1
SKIP = {"supervisor.lock", "control.json"}
IMPORT_REMOTE = "bananavibe-import"


class HandoverError(Exception):
    pass


def _rel(repo: Path, workspace: Path) -> str:
    return str(repo.relative_to(workspace)) if repo.is_relative_to(workspace) else ""


def export(paths: Paths, cfg: Config, output: Path, include_logs: bool = True) -> dict:
    """Write the run to `output`. The supervisor must not be running. Returns the manifest."""
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    manifest = {
        "format": FORMAT, "bananavibe": __version__, "name": cfg.name, "exported_at": iso(now()),
        "host": socket.gethostname(), "workspace": str(paths.workspace), "repos": [],
    }
    with tempfile.TemporaryDirectory(prefix="bananavibe-export-") as tmp:
        tmpdir = Path(tmp)
        for i, repo in enumerate(repos):
            rel = _rel(repo, paths.workspace)
            if not rel:
                raise HandoverError(f"{repo} is outside the workspace; move it inside to export the run")
            if gitops.dirty(repo):
                if not cfg.git_snapshot:
                    raise HandoverError(f"{repo} has uncommitted changes; commit them first (snapshots are off)")
                gitops.snapshot(repo, "bananavibe: work in progress at export")
            if gitops.git(repo, "rev-parse", "--verify", "HEAD").returncode != 0:
                raise HandoverError(f"{repo} has no commits yet")
            bundle = tmpdir / f"{i}.bundle"
            r = gitops.git(repo, "bundle", "create", str(bundle), "--all", timeout=3600)
            if r.returncode != 0:
                raise HandoverError(f"git bundle failed for {repo}: {r.stderr.strip()}")
            url = gitops.git(repo, "remote", "get-url", cfg.git_remote).stdout.strip()
            manifest["repos"].append({"path": rel, "bundle": f"repos/{i}.bundle",
                                      "branch": gitops.current_branch(repo), "head": gitops.head(repo),
                                      "remote": cfg.git_remote if url else "", "url": url})
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz") as tar:
            data = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            info.mtime = int(now().timestamp())
            tar.addfile(info, io.BytesIO(data))

            def keep(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
                parts = Path(ti.name).parts[1:]
                if parts and (parts[0] in SKIP or parts[0] == "doctor"
                              or (not include_logs and parts[0] in ("logs", "prompts"))):
                    return None
                return ti
            tar.add(paths.root, arcname="bananavibe", filter=keep)
            for entry in manifest["repos"]:
                tar.add(tmpdir / Path(entry["bundle"]).name, arcname=entry["bundle"])
    return manifest


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts or not (m.isfile() or m.isdir()):
            raise HandoverError(f"refusing to extract suspicious entry {m.name!r}")
        members.append(m)
    return members


def _restore_repo(dest: Path, bundle: Path, entry: dict, force: bool) -> str:
    """Bring `dest` to the exported state of the repository. Returns a description of what was done."""
    branch = entry["branch"]
    created = not gitops.is_repo(dest)
    if created:
        dest.mkdir(parents=True, exist_ok=True)
        gitops.git(dest, "init", "-q", check=True)
    elif gitops.dirty(dest) and not force:
        raise HandoverError(f"{dest} has uncommitted changes; commit or stash them, or use --force")
    r = gitops.git(dest, "fetch", "-q", "--force", str(bundle),
                   f"refs/heads/*:refs/remotes/{IMPORT_REMOTE}/*", timeout=3600)
    if r.returncode != 0:
        raise HandoverError(f"cannot fetch the bundle into {dest}: {r.stderr.strip()}")
    imported = f"refs/remotes/{IMPORT_REMOTE}/{branch}"
    local = f"refs/heads/{branch}"
    if not created and gitops.git(dest, "rev-parse", "--verify", "-q", local).returncode == 0:
        ff = gitops.git(dest, "merge-base", "--is-ancestor", local, imported).returncode == 0
        if not ff and not force:
            raise HandoverError(f"{dest}: local branch {branch} has commits the export doesn't; "
                                "merge them yourself or use --force to replace it")
    if created:
        # Every exported branch becomes a local branch, as in the original repository.
        for ref in gitops.git(dest, "for-each-ref", "--format=%(refname)",
                              f"refs/remotes/{IMPORT_REMOTE}/").stdout.split():
            name = ref.removeprefix(f"refs/remotes/{IMPORT_REMOTE}/")
            if name != branch:
                gitops.git(dest, "branch", "-q", "-f", name, ref)
    r = gitops.git(dest, "checkout", "-q", *(["-f"] if force else []), "-B", branch, imported)
    if r.returncode != 0:
        raise HandoverError(f"cannot check out {branch} in {dest}: {r.stderr.strip()}")
    if entry.get("url") and entry.get("remote"):
        if gitops.git(dest, "remote", "get-url", entry["remote"]).returncode != 0:
            gitops.git(dest, "remote", "add", entry["remote"], entry["url"])
    return f"{'created' if created else 'updated'} {dest} on {branch} at {gitops.head(dest)[:10]}"


def import_run(archive: Path, workspace: Path, force: bool = False) -> list[str]:
    """Restore an exported run into `workspace`. Returns a log of what was done."""
    paths = Paths(workspace)
    if (paths.state.exists() or paths.config.exists()) and not force:
        raise HandoverError(f"{paths.root} already exists; use --force to replace that run")
    done = []
    with tempfile.TemporaryDirectory(prefix="bananavibe-import-") as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            members = _safe_members(tar)
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmpdir, members=members, filter="data")
            else:  # Python 3.11 before 3.11.4; members are already checked
                tar.extractall(tmpdir, members=members)
        try:
            manifest = json.loads((tmpdir / "manifest.json").read_text())
        except (OSError, ValueError) as e:
            raise HandoverError(f"{archive} is not a BananaVibe export: {e}") from None
        if manifest.get("format") != FORMAT:
            raise HandoverError(f"unsupported export format {manifest.get('format')}")
        workspace.mkdir(parents=True, exist_ok=True)
        # The workspace-root repository first: it may be cloned into the (empty) workspace itself.
        for entry in sorted(manifest["repos"], key=lambda e: e["path"] != "."):
            dest = (paths.workspace / entry["path"]).resolve()
            if not dest.is_relative_to(paths.workspace):
                raise HandoverError(f"repository path {entry['path']!r} escapes the workspace")
            done.append(_restore_repo(dest, tmpdir / entry["bundle"], entry, force))

        if paths.root.exists():
            shutil.rmtree(paths.root)
        shutil.copytree(tmpdir / "bananavibe", paths.root)
        if gitops.is_repo(paths.workspace):
            gitops.exclude_path(paths.workspace, f"/{DIRNAME}/")

    state = State(paths)
    old = manifest["workspace"].rstrip("/")
    state["base_commits"] = {(str(paths.workspace) + k[len(old):] if k == old or k.startswith(old + "/") else k): v
                             for k, v in state["base_commits"].items()}
    if state["status"] in ("running", "waiting", "paused", "crashed"):
        state["status"] = "stopped"
        state["detail"] = f"imported from {manifest['host']}"
    state["pid"] = None
    state["current"] = None
    state["current_agent"] = None
    state["wait_until"] = None
    for info in state["agents"].values():
        # Limits and logins belong to the other machine's accounts; this one starts fresh.
        info["cooldown_until"] = None
        info["consecutive_failures"] = 0
    state.event("imported", f"imported from {manifest['host']}:{manifest['workspace']} "
                            f"(exported {manifest['exported_at']})")
    state.save()
    done.append(f"restored {paths.root}")
    return done
