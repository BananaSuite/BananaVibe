"""Archive Agent configuration, task state, transcripts, and Git checkpoints."""

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from banana_backup.cli import add_commands, handle
from banana_backup.files import atomic_write, digest, directory, private_bytes, publish, write_json
from banana_backup.git import Git
from banana_backup.store import Store

from .config import Config, branch
from .forge import Forge
from .state import StateStore, task_id

TASK_BRANCH = re.compile(r"bananavibe/[0-9a-f]{16}-[1-9][0-9]*")
FILES = {"configuration.toml", "tasks.json", "issues.json", "control.bundle", "target.bundle"}


def git_config(forge, repository, maximum):
    return {"url": forge.config.server_url + "/" + repository + ".git", "forge": forge.config.forge,
            "username": forge.identity.get("login", "git"), "max_mib": maximum}


def capture_bundle(git, refs, output):
    for ref in refs:
        git.run("fetch", "--no-tags", git.url, "refs/heads/" + branch(ref) + ":refs/heads/" + ref, bounded=True)
    if refs:
        git.run("bundle", "create", str(output), "--branches", bounded=True)


def export_package(forge, configuration, destination, *, maximum=512, git_factory=Git):
    destination = Path(destination).absolute()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    states = list(StateStore(forge).all())
    for state in states:
        if (type(state.get("issue")) is not int or state["issue"] <= 0
                or state.get("task_id") != task_id(forge.config.control_repository, state["issue"])
                or state.get("target_repository") != forge.config.target_repository
                or (state.get("branch") and not TASK_BRANCH.fullmatch(state["branch"]))):
            raise ValueError("Invalid saved task state; inspect it before backing up.")
    with tempfile.TemporaryDirectory(prefix="agent-export-", dir=destination.parent) as name:
        work = Path(name)
        payload = directory(work / "payload")
        raw_config = Path(configuration).read_bytes()
        if len(raw_config) > 1024 * 1024:
            raise ValueError("The BananaVibe configuration exceeds 1 MiB.")
        atomic_write(payload / "configuration.toml", raw_config)
        write_json(payload / "tasks.json", states)
        transcripts = []
        total = 0
        for state in states:
            record = {"issue": forge.issue(state["issue"]), "comments": forge.comments(state["issue"])}
            total += len(json.dumps(record).encode())
            if total > maximum * 1024 * 1024:
                raise ValueError("Issue transcripts exceed the configured backup limit.")
            transcripts.append(record)
        write_json(payload / "issues.json", transcripts)
        token = work / "forge.token"
        atomic_write(token, forge.token)
        control = git_factory(directory(work / "control"), git_config(forge, forge.config.control_repository, maximum), token)
        default = branch(forge.repo(forge.config.control_repository)["default_branch"])
        refs = [default]
        if forge.branch_sha(forge.config.control_repository, forge.config.state_branch):
            refs.append(forge.config.state_branch)
        capture_bundle(control, sorted(set(refs)), payload / "control.bundle")
        target = git_factory(directory(work / "target"), git_config(forge, forge.config.target_repository, maximum), token)
        raw = target.run("ls-remote", "--heads", target.url, "refs/heads/bananavibe/*")
        if len(raw) > 1024 * 1024:
            raise ValueError("Too many task branches. Archive old tasks before backing up.")
        task_refs = []
        for line in raw.decode().splitlines():
            _, ref = line.split("\t", 1)
            name = ref.removeprefix("refs/heads/")
            if TASK_BRANCH.fullmatch(name):
                task_refs.append(name)
        for ref in task_refs:
            target.run("fetch", "--no-tags", target.url, "refs/heads/" + ref + ":refs/heads/" + ref, bounded=True)
        # Capture the immutable checkpoint associated with the saved state,
        # even when a running task has since pushed a newer commit.
        for state in states:
            ref, checkpoint = state.get("branch"), state.get("checkpoint")
            if not ref or not checkpoint:
                continue
            if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", checkpoint):
                raise ValueError("Invalid saved task checkpoint.")
            if ref not in task_refs:
                if state.get("status") == "complete":
                    continue  # A human may have deleted the merged PR branch.
                raise ValueError("An unfinished task branch is missing. Recover it before backing up.")
            target.run("cat-file", "-e", checkpoint + "^{commit}")
            target.run("update-ref", "refs/heads/" + ref, checkpoint)
        if task_refs:
            target.run("bundle", "create", str(payload / "target.bundle"), "--branches", bounded=True)
        files = {path.name: {"sha256": digest(path), "bytes": path.stat().st_size} for path in payload.iterdir()}
        if sum(info["bytes"] for info in files.values()) > maximum * 1024 * 1024:
            raise ValueError("The BananaVibe archive exceeds the configured size limit.")
        metadata = {"schema": 1, "product": "BananaVibe", "created_at": datetime.now(timezone.utc).isoformat(),
                    "server_url": forge.config.server_url, "control_repository": forge.config.control_repository,
                    "target_repository": forge.config.target_repository, "files": files}
        temporary = work / "package.tar.gz"
        with tarfile.open(temporary, "w:gz") as archive:
            for path in sorted(payload.iterdir()):
                archive.add(path, arcname=path.name, recursive=False)
            content = json.dumps(metadata, sort_keys=True).encode()
            info = tarfile.TarInfo("metadata.json")
            info.size, info.mode = len(content), 0o600
            archive.addfile(info, io.BytesIO(content))
        temporary.chmod(0o600)
        publish(temporary, destination)
    return destination


def restore_package(package, destination, *, maximum=1024):
    """Restore an offline review directory; do not replay issues or start tasks."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new recovery directory. Existing files are never replaced.")
    if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("The recovery directory cannot traverse symbolic links.")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-restore-", dir=destination.parent) as name:
        work = Path(name)
        seen, total = set(), 0
        with tarfile.open(package, "r|*") as archive:
            for member in archive:
                if not member.isfile() or member.name not in FILES | {"metadata.json"} or member.name in seen or member.size < 0:
                    raise ValueError("Unsafe, duplicate, or unexpected Agent archive entry.")
                total += member.size
                if total > maximum * 1024 * 1024 or (member.name == "metadata.json" and member.size > 65536):
                    raise ValueError("The BananaVibe archive exceeds the extracted-size limit.")
                seen.add(member.name)
                with archive.extractfile(member) as source, (work / member.name).open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                (work / member.name).chmod(0o600)
        metadata = json.loads((work / "metadata.json").read_text())
        if (metadata.get("schema") != 1 or metadata.get("product") != "BananaVibe"
                or set(metadata.get("files", {})) != seen - {"metadata.json"}
                or not {"configuration.toml", "control.bundle", "tasks.json", "issues.json"} <= seen):
            raise ValueError("The BananaVibe archive does not match its manifest.")
        for filename, info in metadata["files"].items():
            if (work / filename).stat().st_size != info.get("bytes") or digest(work / filename) != info.get("sha256"):
                raise ValueError("The BananaVibe archive failed its integrity check.")
        # mkdir is exclusive. Publish into the directory we just created rather
        # than replacing a concurrently created path with rename().
        destination.mkdir(mode=0o700)
        for filename in seen:
            shutil.copyfile(work / filename, destination / filename)
            (destination / filename).chmod(0o600)
    return {"outcome": "restored for review", "directory": str(destination),
            "next": "Review the recovered configuration and transcripts. Recreate runner secrets separately. Use restore-state only with Actions disabled."}


def restore_state(forge, recovery, confirm_repository, *, git_factory=Git):
    """Restore missing checkpoint branches and paused state with optimistic writes."""
    recovery = directory(recovery)
    metadata = json.loads(private_bytes(recovery / "metadata.json", 65536))
    if (confirm_repository != forge.config.control_repository or metadata.get("control_repository") != confirm_repository
            or metadata.get("target_repository") != forge.config.target_repository
            or metadata.get("server_url") != forge.config.server_url):
        raise ValueError("Confirm the original issue repository and matching forge/target configuration.")
    # Recheck the extracted review copy; edits to state or bundles require a
    # deliberate manual recovery, not automatic replay as trusted backup data.
    for name in ("tasks.json", "target.bundle"):
        info = metadata["files"].get(name)
        if info and digest(recovery / name) != info["sha256"]:
            raise ValueError("The recovered state or checkpoint bundle has changed.")
    states = json.loads(private_bytes(recovery / "tasks.json", 64 * 1024 * 1024))
    if not isinstance(states, list) or len(states) >= 1000:
        raise ValueError("Invalid Agent state archive.")
    store, current, missing = StateStore(forge), {}, {}
    for state in states:
        number = state.get("issue")
        if (type(number) is not int or number <= 0 or state.get("schema") != 1
                or state.get("task_id") != task_id(confirm_repository, number)
                or state.get("target_repository") != forge.config.target_repository or number in current):
            raise ValueError("Invalid recovered task record.")
        if not forge.issue(number):
            raise ValueError("An original issue is missing. Recover its transcript manually; issue numbers are not recreated.")
        old, sha = store.read(number)
        if old and old.get("lease_until", 0) > time.time():
            raise ValueError("A task is still running. Disable Actions and wait for leases to expire before restoring.")
        current[number] = {"state": old, "sha": sha}
        ref, checkpoint = state.get("branch"), state.get("checkpoint")
        if ref and checkpoint and state.get("status") != "complete":
            if not TASK_BRANCH.fullmatch(ref) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", checkpoint):
                raise ValueError("Invalid recovered branch or checkpoint.")
            existing = forge.branch_sha(forge.config.target_repository, ref)
            if existing and existing != checkpoint:
                raise ValueError("A task branch has newer or different work. Review it manually; restore does not overwrite it.")
            if not existing:
                missing[ref] = checkpoint
    write_json(recovery / ("before-restore-" + str(time.time_ns()) + ".json"), current)
    if missing:
        if "target.bundle" not in metadata["files"]:
            raise ValueError("The backup has no checkpoint bundle for its unfinished tasks.")
        with tempfile.TemporaryDirectory(prefix=".replay-", dir=recovery) as name:
            work = Path(name)
            token = work / "forge.token"
            atomic_write(token, forge.token)
            git = git_factory(work, git_config(forge, forge.config.target_repository, 1024), token)
            for ref, checkpoint in missing.items():
                git.run("-c", "protocol.file.allow=always", "fetch", "--no-tags", str(recovery / "target.bundle"), "refs/heads/" + ref, bounded=True)
                if git.run("rev-parse", "FETCH_HEAD").decode().strip() != checkpoint:
                    raise ValueError("The bundled checkpoint does not match the saved task state.")
            for ref, checkpoint in missing.items():
                git.push(checkpoint, "refs/heads/" + ref)
    store.ensure()
    restored = []
    for state in states:
        number = state["issue"]
        paused = {**state, "status": "complete" if state.get("status") == "complete" else "paused",
                  "desired": "complete" if state.get("status") == "complete" else "paused",
                  "runner": "", "lease_until": 0, "updated_at": time.time(), "reason": "Restored backup; a maintainer must review and resume."}
        forge.put_content(store.path(number), json.dumps(paused).encode(), current[number]["sha"])
        restored.append(number)
    return {"outcome": "state restored", "issues": restored, "tasks_started": 0}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    command = add_commands(parser.add_subparsers(dest="command", required=True), agent=True)
    command.add_argument("--root", type=Path, default=Path(os.environ.get("BANANAVIBE_BACKUP_ROOT", "~/.local/state/bananavibe-backups")).expanduser())
    command.add_argument("--config", default=".bananavibe.toml")
    args = parser.parse_args(["backups", *argv])
    store = Store(args.root, "BananaVibe")
    try:
        def create():
            config = Config.load(args.config)
            forge = Forge(config, os.environ.get("BANANAVIBE_TOKEN", ""))
            destination = store.root / ("agent-" + str(time.time_ns()) + ".tar.gz")
            return export_package(forge, args.config, destination, maximum=store.settings()["max_mib"])
        result = handle(args, store, create_package=create,
                        restore_package=lambda package, options: restore_package(package, options.output))
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("BananaVibe backups:", str(error), file=sys.stderr)
        return 1


def replay_main(argv):
    parser = argparse.ArgumentParser(description="Restore reviewed Agent state with Actions disabled; no task starts automatically.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--config", default=".bananavibe.toml")
    parser.add_argument("--confirm-repository", required=True)
    args = parser.parse_args(argv)
    try:
        forge = Forge(Config.load(args.config), os.environ.get("BANANAVIBE_TOKEN", ""))
        print(json.dumps(restore_state(forge, args.directory, args.confirm_repository), indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("BananaVibe restore-state:", str(error), file=sys.stderr)
        return 1
