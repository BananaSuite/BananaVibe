import json
import os

import pytest

from bananavibe.workspace import BranchMoved, Workspace, clear_report, read_report
from fakes import git


@pytest.fixture
def workspace(forge, tmp_path):
    sha = forge.branch_sha("team/app", "main")
    forge.create_branch("team/app", "bananavibe/0123456789abcdef-1", sha)
    result = Workspace(tmp_path / "task", forge, "bananavibe/0123456789abcdef-1")
    result.prepare(sha)
    return result, sha


def test_metadata_and_credentials_stay_outside_the_project(workspace):
    ws, _ = workspace
    assert not (ws.path / ".git").exists()
    assert (ws.path / "app.py").read_text() == "answer = 1\n"
    assert "fixture-token" not in " ".join(str(path.read_text(errors="ignore")) for path in ws.path.rglob("*")
                                           if path.is_file())


def test_snapshot_commits_changes_but_not_caches(workspace, forge):
    ws, base = workspace
    (ws.path / "app.py").write_text("answer = 2\n")
    (ws.path / "__pycache__").mkdir()
    (ws.path / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"cache")
    (ws.path / ".pytest_cache").mkdir()
    (ws.path / ".pytest_cache" / "v").write_text("x")
    (ws.path / ".bananavibe-task" / "result.json").write_text("{}")
    assert ws.snapshot() == {}
    head = ws.push()
    assert forge.branch_sha("team/app", ws.branch) == head != base
    assert ws.changed_files(base) == ["app.py"]
    assert forge.show(ws.branch, "app.py") == "answer = 2"


def test_protected_paths_and_embedded_repositories_are_reverted(workspace):
    ws, base = workspace
    workflow = ws.path / ".github" / "workflows" / "steal.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on: push\n")
    (ws.path / ".bananavibe.toml").write_text("forged = true\n")
    nested = ws.path / "vendor" / "lib"
    nested.mkdir(parents=True)
    git("init", "-q", str(nested))
    (nested / "x").write_text("x")
    git("add", ".", cwd=nested)
    git("commit", "-q", "-m", "x", cwd=nested)
    (ws.path / "app.py").write_text("answer = 3\n")
    rejected = ws.snapshot()
    assert rejected == {".github/workflows/steal.yml": "protected path", ".bananavibe.toml": "protected path",
                        "vendor/lib": "embedded Git repository"}
    assert not workflow.exists() and not (ws.path / ".bananavibe.toml").exists()
    assert ws.changed_files(base) == ["app.py"]


def test_push_refuses_to_overwrite_outside_changes(workspace, forge, tmp_path):
    ws, base = workspace
    other = tmp_path / "other"
    git("clone", "-q", "-b", ws.branch, str(forge.target), str(other))
    (other / "app.py").write_text("answer = 99\n")
    git("commit", "-qam", "outside", cwd=other)
    git("push", "-q", "origin", ws.branch, cwd=other)
    (ws.path / "app.py").write_text("answer = 2\n")
    ws.snapshot()
    with pytest.raises(BranchMoved):
        ws.push()
    assert forge.show(ws.branch, "app.py") == "answer = 99"


def test_prepare_detects_a_branch_that_moved_since_the_checkpoint(forge, tmp_path):
    sha = forge.branch_sha("team/app", "main")
    forge.create_branch("team/app", "bananavibe/0123456789abcdef-1", sha)
    ws = Workspace(tmp_path / "task", forge, "bananavibe/0123456789abcdef-1")
    with pytest.raises(BranchMoved):
        ws.prepare("0" * 40)


def test_export_contains_only_the_committed_tree(workspace, tmp_path):
    ws, _ = workspace
    (ws.path / "app.py").write_text("answer = 2\n")
    ws.snapshot()
    (ws.path / "app.py").write_text("uncommitted\n")
    (ws.path / "scratch.txt").write_text("untracked\n")
    tree = ws.export(tmp_path / "tree")
    assert (tree / "app.py").read_text() == "answer = 2\n"
    assert not (tree / "scratch.txt").exists()


def test_whitespace_errors_are_reported(workspace):
    ws, base = workspace
    (ws.path / "app.py").write_text("answer = 2   \n")
    ws.snapshot()
    assert "trailing whitespace" in ws.whitespace_errors(base)


def test_reports_are_read_without_following_links(tmp_path):
    project, outside = tmp_path / "project", tmp_path / "controller"
    project.mkdir()
    outside.mkdir()
    (outside / "result.json").write_text(json.dumps({"status": "complete"}))
    (project / ".bananavibe-task").symlink_to(outside, target_is_directory=True)
    assert read_report(project, "result.json", 1000) is None
    with pytest.raises(OSError):
        clear_report(project)
    assert (outside / "result.json").exists()


def test_report_rejects_links_fifos_and_oversized_files(tmp_path):
    reports = tmp_path / ".bananavibe-task"
    reports.mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"status": "complete"}')
    (reports / "result.json").symlink_to(secret)
    assert read_report(tmp_path, "result.json", 100) is None
    clear_report(tmp_path)
    assert secret.exists()
    os.mkfifo(reports / "result.json")
    assert read_report(tmp_path, "result.json", 100) is None
    clear_report(tmp_path)
    (reports / "result.json").write_text(json.dumps({"body": "x" * 200}))
    assert read_report(tmp_path, "result.json", 100) is None
    (reports / "result.json").write_text('{"status": "complete"}')
    assert read_report(tmp_path, "result.json", 100) == {"status": "complete"}


def test_git_ignores_repository_hooks_and_global_config(workspace, monkeypatch, tmp_path):
    ws, _ = workspace
    marker = tmp_path / "hook-ran"
    hook = ws.root / "repository.git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "evil"))
    (ws.path / "app.py").write_text("answer = 5\n")
    ws.snapshot()
    assert not marker.exists()
    assert ws.git.environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert [key for key, value in ws.git.environment.items() if "fixture-token" in value] == ["BANANAVIBE_GIT_PASSWORD"]
