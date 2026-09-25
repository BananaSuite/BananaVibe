"""Recover Agent state and Git checkpoints without restarting work or posting."""

import io
import json
from pathlib import Path
import tarfile
import time

import pytest

from banana_backup.git import Git
from bananavibe import backups
from bananavibe.state import StateStore, task_id
from test_tasks import LocalForge


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    forge = LocalForge(tmp_path)
    control = tmp_path / "control.git"
    forge.exec("git", "clone", "--bare", str(forge.remote), str(control))
    control_sha = forge.branch_sha(forge.config.control_repository, "main")
    forge.exec("git", "--git-dir", str(control), "update-ref", "refs/heads/bananavibe-state", control_sha)
    forge.control_branches["bananavibe-state"] = control_sha
    identifier = task_id(forge.config.control_repository, 7)
    ref = "bananavibe/" + identifier + "-1"
    forge.create_branch(forge.config.target_repository, ref, control_sha)
    state = {"schema": 1, "issue": 7, "task_id": identifier, "target_repository": forge.config.target_repository,
             "base_branch": "main", "generation": 1, "branch": ref, "base_sha": control_sha, "checkpoint": control_sha,
             "model": "coding", "status": "running", "desired": "running", "runner": "old-runner",
             "lease_until": time.time() + 300, "request": 1, "operation": "continue", "guidance": [], "events": [], "pr_url": ""}
    forge.put_content(StateStore(forge).path(7), json.dumps(state).encode())
    monkeypatch.setattr(StateStore, "all", lambda _: iter([state]))
    monkeypatch.setattr(forge, "comments", lambda _: [{"body": "private maintainer guidance"}], raising=False)
    configuration = tmp_path / "configuration.toml"
    configuration.write_text('control_repository = "maintainers/prompts"\n')
    def factory(work, config, token_file):
        remote = control if config["url"].endswith("/" + forge.config.control_repository + ".git") else forge.remote
        return Git(work, {**config, "url": str(remote)}, token_file, allow_local=True)
    return forge, configuration, state, factory


def test_agent_backup_restores_private_transcripts_configuration_and_checkpoints(fixture, tmp_path):
    forge, configuration, state, factory = fixture
    package = backups.export_package(forge, configuration, tmp_path / "agent.tar.gz", git_factory=factory)
    result = backups.restore_package(package, tmp_path / "recovery")
    root = Path(result["directory"])
    assert (root / "configuration.toml").read_text() == configuration.read_text()
    assert json.loads((root / "tasks.json").read_text())[0]["checkpoint"] == state["checkpoint"]
    assert "private maintainer guidance" in (root / "issues.json").read_text()
    assert (root / "target.bundle").stat().st_size > 0
    assert (root / "control.bundle").stat().st_size > 0
    assert "forge.token" not in json.loads((root / "metadata.json").read_text())["files"]
    assert forge.posts == []
    assert StateStore(forge).read(7)[0]["runner"] == "old-runner"


def test_state_restore_rejects_active_lease_then_restores_missing_branch_paused(fixture, tmp_path):
    forge, configuration, state, factory = fixture
    package = backups.export_package(forge, configuration, tmp_path / "agent.tar.gz", git_factory=factory)
    backups.restore_package(package, tmp_path / "recovery")
    with pytest.raises(ValueError, match="still running"):
        backups.restore_state(forge, tmp_path / "recovery", forge.config.control_repository, git_factory=factory)
    forge.records.clear()
    forge.exec("git", "--git-dir", str(forge.remote), "update-ref", "-d", "refs/heads/" + state["branch"])
    result = backups.restore_state(forge, tmp_path / "recovery", forge.config.control_repository, git_factory=factory)
    restored, _ = StateStore(forge).read(7)
    assert result["tasks_started"] == 0
    assert restored["status"] == "paused" and restored["desired"] == "paused"
    assert restored["runner"] == "" and restored["lease_until"] == 0
    assert forge.branch_sha(forge.config.target_repository, state["branch"]) == state["checkpoint"]
    assert forge.posts == []
    assert list((tmp_path / "recovery").glob("before-restore-*.json"))


def test_state_restore_keeps_newer_work_and_checks_original_repository(fixture, tmp_path):
    forge, configuration, state, factory = fixture
    package = backups.export_package(forge, configuration, tmp_path / "agent.tar.gz", git_factory=factory)
    backups.restore_package(package, tmp_path / "recovery")
    with pytest.raises(ValueError, match="original issue repository"):
        backups.restore_state(forge, tmp_path / "recovery", "another/repository", git_factory=factory)
    forge.records.clear()
    seed = tmp_path / "seed"
    (seed / "app.py").write_text("answer = 99\n")
    forge.exec("git", "-C", str(seed), "add", ".")
    forge.exec("git", "-C", str(seed), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "newer work")
    forge.exec("git", "-C", str(seed), "push", str(forge.remote), "HEAD:refs/heads/" + state["branch"])
    latest = forge.branch_sha(forge.config.target_repository, state["branch"])
    with pytest.raises(ValueError, match="newer or different work"):
        backups.restore_state(forge, tmp_path / "recovery", forge.config.control_repository, git_factory=factory)
    assert forge.branch_sha(forge.config.target_repository, state["branch"]) == latest
    assert StateStore(forge).read(7) == (None, None)


@pytest.mark.parametrize("name,kind", [("../outside", tarfile.REGTYPE), ("tasks.json", tarfile.SYMTYPE), (".git/hooks/post-checkout", tarfile.REGTYPE)])
def test_agent_restore_rejects_links_and_paths_outside_the_archive_contract(tmp_path, name, kind):
    package = tmp_path / "unsafe.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.type = kind
        member.linkname = "/tmp/outside"
        member.size = 1 if kind == tarfile.REGTYPE else 0
        archive.addfile(member, io.BytesIO(b"x") if member.size else None)
    with pytest.raises(ValueError, match="Unsafe"):
        backups.restore_package(package, tmp_path / "recovered")
    assert not (tmp_path / "recovered").exists()
