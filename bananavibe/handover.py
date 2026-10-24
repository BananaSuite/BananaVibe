"""Moving a run to another machine: `bananavibe export` and `bananavibe import`.

An export is one .tar.gz with:
- `manifest.json`: where the run came from, which repositories it manages, checksums;
- `bananavibe/`: the run directory (mission, plan, notes, journal, state, reports, and optionally logs);
- `repos/<n>.bundle`: a `git bundle` of every repository (all branches and tags), so the work branch with every
  snapshot travels without needing a shared remote;
- `agent-sessions/`: the conversation of an interrupted Claude Code session, so it can be resumed natively
  on the new machine (best effort: if that fails, a fresh session continues from a summary).

Import recreates the repositories (or updates existing clones), restores `.bananavibe/`, and rewrites the
state so `bananavibe start` continues exactly where the run stopped, including an interrupted session.
Uncommitted and git-ignored files (build output, node_modules, local databases) do not travel.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import tarfile
import tempfile
from pathlib import Path

from bananavibe import __version__, gitops, housekeeping
from bananavibe.config import Config, ConfigError, load
from bananavibe.state import DIRNAME, Paths, State
from bananavibe.util import iso, now, read_text

FORMAT = 1
# Machine-specific files that must not travel: locks, live control, process info, local service setup.
SKIP = {"supervisor.lock", "guardian.lock", "guardian.json", "heartbeat.json", "control.json", ".control.lock",
        ".answers.lock", "service.json", "service.env", "autostart.sh", "doctor", "state.json.bak", ".git"}
IMPORT_REMOTE = "bananavibe-import"
URL_OK = re.compile(r"^(https?://|ssh://|git://|git@|[\w.-]+@[\w.-]+:|file://|/)", re.I)
# The workspace itself or a folder inside it, with no hidden (.git, .bananavibe...) or parent components.
REPO_PATH = re.compile(r"^(\.|[^./\\][^/\\]*(/[^./\\][^/\\]*)*)$")
BUNDLE = re.compile(r"^repos/\d+\.bundle$")
SESSION_FILE = re.compile(r"^agent-sessions/[A-Za-z0-9_-]{6,100}\.jsonl$")


class HandoverError(Exception):
    pass


def _rel(repo: Path, workspace: Path) -> str:
    return str(repo.relative_to(workspace)) if repo.is_relative_to(workspace) else ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def claude_dir(cfg: Config | None, agent: str) -> Path:
    env = (cfg.agents[agent].env if cfg and agent in cfg.agents else {}) or {}
    base = env.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base).expanduser()


def claude_project_name(workspace: Path | str) -> str:
    """How Claude Code names its per-directory history folder (`/root/work` -> `-root-work`)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(workspace))


def _find_claude_session(cfg: Config, agent: str, session_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,100}", session_id or ""):
        return None
    projects = claude_dir(cfg, agent) / "projects"
    if not projects.is_dir():
        return None
    matches = sorted(projects.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def _secret_warnings(cfg: Config) -> list[str]:
    out = []
    for name, agent in cfg.agents.items():
        secrets = [k for k in agent.env if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)]
        if secrets:
            out.append(f"config.toml contains secrets for agent {name} ({', '.join(secrets)}); the archive includes "
                       "them, so keep it private")
    return out


def _settle_lost_session(paths: Paths, cfg: Config | None) -> None:
    """If the supervisor died during a session, record that session as interrupted (so it is continued, and
    its conversation can travel), after making sure its agent is not still running."""
    from bananavibe.supervisor import recover_protected_files, stop_orphaned_session  # late: avoids an import cycle
    state = State(paths)
    cur = state["current"]
    if not cur:
        return
    stop_orphaned_session(cur)
    recover_protected_files(paths)  # undo what the cut-off session's agent changed in GOAL.md / config.toml
    state = State(paths)
    cur = state["current"]
    activity: list[str] = []
    agent = cur.get("agent", "")
    if cfg and agent in cfg.agents and cur.get("log"):
        from bananavibe.adapters import make_adapter
        from bananavibe.util import read_tail
        parser = make_adapter(cfg.agents[agent]).parser()
        for line in read_tail(paths.workspace / cur["log"], 400_000).splitlines():
            if not line.startswith("# "):
                activity += parser.feed(line)
        cur["session_id"] = cur.get("session_id") or parser.session_id
    state["interrupted"] = {"label": cur.get("label", "work"), "agent": agent, "host": socket.gethostname(),
                            "session_id": cur.get("session_id", ""), "started": cur.get("started"), "at": iso(now()),
                            "activity": [str(a) for a in activity[-30:]],
                            "reason": "the supervisor stopped unexpectedly"}
    state["current"] = None
    state["current_agent"] = None
    state.save()


def export(paths: Paths, cfg: Config, output: Path, include_logs: bool = True) -> dict:
    """Write the run to `output`. The supervisor must not be running. Returns the manifest."""
    repos = gitops.discover(paths.workspace, cfg.git_repos)
    _settle_lost_session(paths, cfg)
    state = State(paths)
    manifest = {
        "format": FORMAT, "bananavibe": __version__, "name": cfg.name, "exported_at": iso(now()),
        "host": socket.gethostname(), "workspace": str(paths.workspace), "repos": [], "agent_sessions": [],
        "warnings": _secret_warnings(cfg),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bananavibe-export-") as tmp:
        tmpdir = Path(tmp)
        for i, repo in enumerate(repos):
            rel = _rel(repo, paths.workspace)
            if not rel:
                raise HandoverError(f"{repo} is outside the workspace; move it inside to export the run")
            if gitops.dirty(repo):
                if not cfg.git_snapshot:
                    raise HandoverError(f"{repo} has uncommitted changes; commit them first (snapshots are off)")
                skip = (DIRNAME,) if repo == paths.workspace else ()
                res = gitops.commit_all(repo, "bananavibe: work in progress at export", cfg.git_max_file_mb,
                                        exclude=skip)
                if res.error:
                    raise HandoverError(f"cannot commit the work in {repo}: {res.error}")
                if res.skipped:
                    manifest["warnings"].append(f"{repo.name}: files over {cfg.git_max_file_mb:g} MB were not "
                                                f"included: {', '.join(res.skipped[:10])}")
            if gitops.git(repo, "rev-parse", "--verify", "HEAD").returncode != 0:
                raise HandoverError(f"{repo} has no commits yet")
            bundle = tmpdir / f"{i}.bundle"
            r = gitops.git(repo, "bundle", "create", str(bundle), "--branches", "--tags", "HEAD", timeout=3600)
            if r.returncode != 0:
                raise HandoverError(f"git bundle failed for {repo}: {r.stderr.strip()}")
            url = gitops.git(repo, "remote", "get-url", cfg.git_remote).stdout.strip()
            manifest["repos"].append({"path": rel, "bundle": f"repos/{i}.bundle", "sha256": _sha256(bundle),
                                      "branch": gitops.current_branch(repo), "head": gitops.head(repo),
                                      "remote": cfg.git_remote if url else "", "url": url})
        intr = state["interrupted"] or {}
        agent = intr.get("agent", "")
        if intr.get("session_id") and agent in cfg.agents and cfg.agents[agent].type == "claude":
            found = _find_claude_session(cfg, agent, intr["session_id"])
            if found:
                manifest["agent_sessions"].append({
                    "agent": agent, "type": "claude", "session_id": intr["session_id"],
                    "project_dir": found.parent.name, "file": f"agent-sessions/{intr['session_id']}.jsonl"})
        partial = output.with_name(f".{output.name}.partial")
        try:
            with tarfile.open(partial, "w:gz") as tar:
                data = json.dumps(manifest, indent=2).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size = len(data)
                info.mtime = int(now().timestamp())
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))

                def keep(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
                    parts = Path(ti.name).parts[1:]
                    if not (ti.isfile() or ti.isdir()):
                        return None  # no symlinks or devices in an archive meant to be imported elsewhere
                    if parts and (parts[0] in SKIP or parts[0].endswith(".tmp")
                                  or (not include_logs and parts[0] in ("logs", "prompts"))):
                        return None
                    return ti
                tar.add(paths.root, arcname="bananavibe", filter=keep)
                for entry in manifest["repos"]:
                    tar.add(tmpdir / Path(entry["bundle"]).name, arcname=entry["bundle"])
                for entry in manifest["agent_sessions"]:
                    found = _find_claude_session(cfg, entry["agent"], entry["session_id"])
                    if found:
                        tar.add(found, arcname=entry["file"])
            os.replace(partial, output)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    # The run now lives on (or is about to move to) another machine: don't let this one restart it by itself.
    state = State(paths)
    state["operator_stop"] = True
    state.event("exported", f"exported to {output}")
    state.save()
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
    if any(part in (".git", DIRNAME) for part in dest.parts):
        raise HandoverError(f"refusing to restore a repository inside {dest}")
    created = not gitops.is_repo(dest)
    if created:
        dest.mkdir(parents=True, exist_ok=True)
        gitops.git(dest, "init", "-q", check=True)
    elif gitops.dirty(dest) and not force:
        raise HandoverError(f"{dest} has uncommitted changes; commit or stash them, or use --force")
    if not gitops.valid_branch_name(dest, branch):
        raise HandoverError(f"the archive names an invalid branch {branch!r}")
    r = gitops.git(dest, "bundle", "verify", str(bundle), timeout=3600)
    if r.returncode != 0:
        raise HandoverError(f"the git bundle for {entry['path']} is damaged: {r.stderr.strip()[-300:]}")
    r = gitops.git(dest, "fetch", "-q", "--force", str(bundle),
                   f"refs/heads/*:refs/remotes/{IMPORT_REMOTE}/*", timeout=3600)
    if r.returncode != 0:
        raise HandoverError(f"cannot fetch the bundle into {dest}: {r.stderr.strip()}")
    gitops.git(dest, "fetch", "-q", str(bundle), "refs/tags/*:refs/tags/*", timeout=3600)
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
            if name != branch and gitops.valid_branch_name(dest, name):
                gitops.git(dest, "branch", "-q", "-f", name, ref)
    r = gitops.git(dest, "checkout", "-q", *(["-f"] if force else []), "-B", branch, imported)
    if r.returncode != 0:
        raise HandoverError(f"cannot check out {branch} in {dest}: {r.stderr.strip()}")
    url, remote = entry.get("url") or "", entry.get("remote") or ""
    if url and remote and URL_OK.match(url) and re.fullmatch(r"[\w.-]+", remote):
        if gitops.git(dest, "remote", "get-url", remote).returncode != 0:
            gitops.git(dest, "remote", "add", remote, url)
    return f"{'created' if created else 'updated'} {dest} on {branch} at {gitops.head(dest)[:10]}"


def _restore_claude_session(tmpdir: Path, entry: dict, manifest: dict, workspace: Path, cfg: Config | None) -> str:
    sid = entry.get("session_id", "")
    if not (isinstance(entry.get("file"), str) and SESSION_FILE.match(entry["file"])
            and re.fullmatch(r"[A-Za-z0-9_-]{6,100}", sid or "") and entry["file"].endswith(f"/{sid}.jsonl")):
        return ""
    src = tmpdir / entry["file"]
    if not src.is_file():
        return ""
    old_ws = manifest["workspace"].rstrip("/")
    if entry.get("project_dir") != claude_project_name(old_ws):
        return ""  # Claude Code names its folders differently than we know: don't guess
    target_dir = claude_dir(cfg, entry["agent"]) / "projects" / claude_project_name(workspace)
    target = target_dir / f"{sid}.jsonl"
    if target.exists():
        return "the interrupted Claude Code conversation is already here"
    lines = []
    for line in read_text(src).splitlines():
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and obj.get("cwd") == old_ws:
                obj["cwd"] = str(workspace)
                line = json.dumps(obj, ensure_ascii=False)
        except ValueError:
            pass
        lines.append(line)
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"restored the interrupted Claude Code conversation {sid} (it is resumed natively)"


def import_run(archive: Path, workspace: Path, force: bool = False) -> list[str]:
    """Restore an exported run into `workspace`. Returns a log of what was done."""
    paths = Paths(workspace)
    if (paths.state.exists() or paths.config.exists()) and not force:
        raise HandoverError(f"{paths.root} already exists; use --force to replace that run")
    if not archive.is_file():
        raise HandoverError(f"{archive} not found")
    done = []
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bananavibe-import-", dir=_tmp_parent(workspace)) as tmp:
        tmpdir = Path(tmp)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                members = _safe_members(tar)
                need = sum(m.size for m in members) / 1e6
                free = housekeeping.disk_free_mb(tmpdir)
                if free is not None and need * 2.2 > free:
                    raise HandoverError(f"not enough disk space: the archive needs about {need * 2.2:.0f} MB, "
                                        f"{free:.0f} MB free")
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(tmpdir, members=members, filter="data")
                else:  # Python 3.11 before 3.11.4; members are already checked
                    tar.extractall(tmpdir, members=members)
        except (tarfile.TarError, EOFError, OSError) as e:
            raise HandoverError(f"{archive} is not a readable BananaVibe export: {e}") from None
        try:
            manifest = json.loads((tmpdir / "manifest.json").read_text())
        except (OSError, ValueError) as e:
            raise HandoverError(f"{archive} is not a BananaVibe export: {e}") from None
        if manifest.get("format") != FORMAT:
            raise HandoverError(f"unsupported export format {manifest.get('format')}")
        if not (tmpdir / "bananavibe").is_dir():
            raise HandoverError(f"{archive} has no run directory")
        for entry in manifest["repos"]:
            # Repositories may only be the workspace or its direct subfolders (as when they were found), never
            # something like `.git/hooks` or `.bananavibe`, and files must come from the archive itself.
            if not (isinstance(entry.get("path"), str) and REPO_PATH.match(entry["path"])
                    and isinstance(entry.get("bundle"), str) and BUNDLE.match(entry["bundle"])
                    and isinstance(entry.get("branch"), str)):
                raise HandoverError(f"refusing a suspicious repository entry in the archive: {entry!r:.200}")
            bundle = tmpdir / entry["bundle"]
            if not bundle.is_file():
                raise HandoverError(f"the archive is missing {entry['bundle']}")
            if entry.get("sha256") and _sha256(bundle) != entry["sha256"]:
                raise HandoverError(f"{entry['bundle']} is corrupted (checksum mismatch): copy the archive again")
        # The workspace-root repository first: it may be cloned into the (empty) workspace itself.
        for entry in sorted(manifest["repos"], key=lambda e: e["path"] != "."):
            dest = (paths.workspace / entry["path"]).resolve()
            if not dest.is_relative_to(paths.workspace) or dest.name == DIRNAME:
                raise HandoverError(f"repository path {entry['path']!r} escapes the workspace")
            done.append(_restore_repo(dest, tmpdir / entry["bundle"], entry, force))

        # Swap the run directory in without a moment where neither the old nor the new one is complete.
        staging = paths.workspace / f"{DIRNAME}.importing"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(tmpdir / "bananavibe", staging)
        old = None
        if paths.root.exists():
            old = paths.workspace / f"{DIRNAME}.replaced-{now():%Y%m%d%H%M%S}"
            os.replace(paths.root, old)
        os.replace(staging, paths.root)
        if old:
            shutil.rmtree(old, ignore_errors=True)
        if gitops.is_repo(paths.workspace):
            gitops.exclude_path(paths.workspace, f"/{DIRNAME}/")
            gitops.exclude_path(paths.workspace, f"/{DIRNAME}.*/")

        try:
            cfg = load(paths.config, paths.workspace)
        except ConfigError as e:
            cfg = None
            done.append(f"warning: the imported config.toml has a problem here: {e}")
        state = State(paths)
        if state["current"]:  # exported from a run whose supervisor had died mid-session (older versions)
            cur = state["current"]
            state["interrupted"] = {"label": cur.get("label", "work"), "agent": cur.get("agent", ""),
                                    "host": manifest.get("host", ""), "session_id": cur.get("session_id", ""),
                                    "started": cur.get("started"), "at": iso(now()), "activity": [],
                                    "reason": "the supervisor stopped unexpectedly"}
        for entry in manifest.get("agent_sessions", []):
            try:
                msg = _restore_claude_session(tmpdir, entry, manifest, paths.workspace, cfg)
            except (OSError, KeyError) as e:
                msg = f"could not restore the interrupted conversation ({e}); a fresh session will continue it"
            if msg:
                done.append(msg)
                if "restored" in msg or "already here" in msg:
                    intr = state["interrupted"]
                    if intr and intr.get("session_id") == entry.get("session_id"):
                        intr["host"] = socket.gethostname()

    old_ws = manifest["workspace"].rstrip("/")
    state["base_commits"] = {(str(paths.workspace) + k[len(old_ws):] if k == old_ws or k.startswith(old_ws + "/")
                              else k): v for k, v in state["base_commits"].items()}
    if state["status"] in ("running", "waiting", "paused", "crashed"):
        state["status"] = "stopped"
        state["detail"] = f"imported from {manifest['host']}"
    state["pid"] = None
    state["current"] = None
    state["current_agent"] = None
    state["wait_until"] = None
    state["operator_stop"] = False
    for info in state["agents"].values():
        # Limits and logins belong to the other machine's accounts; this one starts fresh.
        info["cooldown_until"] = None
        info["consecutive_failures"] = 0
    state.event("imported", f"imported from {manifest['host']}:{manifest['workspace']} "
                            f"(exported {manifest['exported_at']})")
    state.save(backup=True)
    housekeeping.protect_run_dir(paths)
    housekeeping.clear_backup(paths, everything=True)  # whatever run was here before must never come back
    housekeeping.backup_memory(paths)
    done.append(f"restored {paths.root}")
    if cfg:
        commands = [f"[notify] command: {cfg.notify_command}"] if cfg.notify_command else []
        commands += [f"[agents.{n}] command: {' '.join(a.command)}" for n, a in cfg.agents.items()
                     if a.type == "custom"]
        if commands:
            done.append("review these commands from the imported config before starting (they will run here): "
                        + "; ".join(commands))
    return done


def _tmp_parent(workspace: Path) -> str | None:
    """Extract next to the workspace (same filesystem, so the final moves are renames) when possible."""
    parent = workspace.parent
    return str(parent) if os.access(parent, os.W_OK) else None
