import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from bananavibe.commands import Command, parse
from bananavibe.config import Config, ConfigurationError, Model
from bananavibe.controller import Controller
from bananavibe.forge import Conflict
from bananavibe.runner import TaskRunner
from bananavibe.state import StateStore
from bananavibe.workspace import Workspace


class LocalForge:
    """An actual local Git remote plus optimistic API state and issue records."""
    def __init__(self, root):
        self.config = Config("github", "https://forge.example", "https://forge.example/api", "maintainers/prompts", "suite/BananaWiki", "main",
                             {"coding": Model("coding", "openai-compatible", "test", "https://models.example/v1", ""),
                              "other": Model("other", "openai-compatible", "second", "https://models.example/v1", "")},
                             "coding", [], [["python3", "-m", "pytest"]], max_iterations=2)
        self.identity = {"login": "bot", "id": 1}
        self.token = "fixture-token"
        self.records, self.posts, self.pulls = {}, [], []
        self.closed = False
        self.remote = root / "target.git"
        source = root / "seed"
        source.mkdir()
        self.exec("git", "init", "-b", "main", str(source))
        (source / "app.py").write_text("answer = 1\n")
        (source / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n")
        self.exec("git", "-C", str(source), "add", ".")
        self.exec("git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Initial")
        self.exec("git", "clone", "--bare", str(source), str(self.remote))
        self.control_branches = {"main": self.branch_sha(self.config.target_repository, "main")}

    def exec(self, *args):
        return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    def repo(self, name):
        return {"default_branch": "main", "permissions": {"push": True}}

    def branch_sha(self, repo, name):
        if repo == self.config.control_repository:
            return self.control_branches.get(name)
        result = subprocess.run(["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "refs/heads/" + name], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    def create_branch(self, repo, name, sha, **kwargs):
        if self.branch_sha(repo, name):
            raise Conflict(409, "POST")
        if repo == self.config.control_repository:
            self.control_branches[name] = sha
        else:
            self.exec("git", "--git-dir", str(self.remote), "update-ref", "refs/heads/" + name, sha)

    def content(self, path):
        return self.records.get(path)

    def put_content(self, path, raw, sha=None):
        old = self.records.get(path)
        if (old and old['sha'] != sha) or (not old and sha):
            raise Conflict(409, "PUT")
        self.records[path] = {"sha": hashlib.sha256(raw).hexdigest(), "content": base64.b64encode(raw).decode()}

    def issue(self, number):
        return {"number": number, "title": "PRIVATE title", "body": "PRIVATE prompt: change the answer to 2"}

    def authorized(self, actor):
        return actor == "maintainer"

    def comment(self, number, body):
        self.posts.append(body)

    def reopen_issue(self, number):
        self.closed = False

    def close_issue(self, number):
        self.closed = True

    def clone_url(self):
        return str(self.remote)

    def pull_request(self, branch, title, body):
        self.pulls.append({"branch": branch, "title": title, "body": body})
        return {"html_url": "https://forge.example/suite/BananaWiki/pull/1"}


@pytest.fixture
def forge(tmp_path):
    return LocalForge(tmp_path)


class ModelFixture:
    prompts = []
    validation_failures = 0
    blocked = False
    control = None
    exit_control = None

    def __init__(self, config, model, workspace, pulse):
        self.path, self.pulse = Path(workspace), pulse

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if type(self).exit_control:
            callback = type(self).exit_control
            type(self).exit_control = None
            callback()

    def freeze(self):
        pass

    def thaw(self):
        pass

    def prompt(self, prompt):
        type(self).prompts.append(prompt)
        task = self.path / ".bananavibe-task"
        task.mkdir(exist_ok=True)
        if (self.path / "public.diff").exists():
            assert "PRIVATE" not in prompt
            assert not (self.path / "app.py").exists()
            (task / "summary.json").write_text(json.dumps({"title": "Correct the answer", "body": "Changes the answer from 1 to 2."}))
        else:
            assert not (self.path / ".git").exists()
            (self.path / "app.py").write_text("answer = 2\n")
            (task / "result.json").write_text(json.dumps({"status": "blocked" if self.blocked else "complete", "question": "Which behavior should be preserved?"}))
        return {"status": "idle"}

    def command(self, command, **kwargs):
        if type(self).control:
            type(self).control()
            type(self).control = None
        self.pulse("Validating")
        if type(self).validation_failures:
            type(self).validation_failures -= 1
            return 1, "Expected answer was incorrect"
        return 0, "passed"


@pytest.fixture(autouse=True)
def clear_engine():
    ModelFixture.prompts = []
    ModelFixture.validation_failures = 0
    ModelFixture.blocked = False
    ModelFixture.control = None
    ModelFixture.exit_control = None


def start(forge):
    controller = Controller(forge)
    number = controller.command(3, Command("start"), "maintainer", "first")
    assert number == 3
    return controller


def run(forge, controller):
    runner = TaskRunner(forge, controller.store, 3, engine_factory=ModelFixture)
    return runner, runner.run()


def test_complete_task_checkpoints_validates_publishes_then_closes(forge):
    controller = start(forge)
    runner, status = run(forge, controller)
    assert status == "complete"
    state, _ = controller.store.read(3)
    assert state['status'] == "complete" and not state['runner']
    assert forge.closed and len(forge.pulls) == 1
    assert state['checkpoint'] == forge.branch_sha(forge.config.target_repository, state['branch'])
    assert "PRIVATE" not in json.dumps(forge.pulls)
    assert "PRIVATE prompt" in ModelFixture.prompts[0]
    assert "public.diff" in ModelFixture.prompts[-1]
    assert forge.exec("git", "--git-dir", str(forge.remote), "show", state['branch'] + ":app.py") == "answer = 2"


def test_failed_checks_loop_before_pr(forge):
    controller = start(forge)
    ModelFixture.validation_failures = 1
    _, status = run(forge, controller)
    assert status == "complete"
    assert any("acceptance checks failed" in prompt for prompt in ModelFixture.prompts)
    assert len(forge.pulls) == 1


def test_exhausted_budget_is_resumable_and_keeps_issue_open(forge):
    controller = start(forge)
    ModelFixture.validation_failures = 9
    _, status = run(forge, controller)
    assert status == "paused"
    state, _ = controller.store.read(3)
    assert state['checkpoint'] != state['base_sha']
    assert not forge.closed and not forge.pulls
    ModelFixture.validation_failures = 0
    controller.command(3, Command("resume"), "maintainer", "resume1")
    _, status = run(forge, controller)
    assert status == "complete"


def test_blocked_task_asks_for_help_and_answer_resumes(forge):
    controller = start(forge)
    ModelFixture.blocked = True
    _, status = run(forge, controller)
    assert status == "blocked"
    assert any("Which behavior" in text for text in forge.posts)
    assert not forge.closed and not forge.pulls
    controller.command(3, Command("answer", "Preserve the old API"), "maintainer", "answer1")
    ModelFixture.blocked = False
    _, status = run(forge, controller)
    assert status == "complete"
    assert any("Preserve the old API" in text for text in ModelFixture.prompts)
    assert "Preserve the old API" not in json.dumps(forge.pulls)


def test_duplicate_event_and_concurrent_claim_do_not_run_twice(forge):
    controller = start(forge)
    assert controller.command(3, Command("start"), "maintainer", "first") is None
    assert controller.store.claim(3, "one")
    assert controller.store.claim(3, "two") is None
    assert controller.store.read(3)[0]['runner'] == "one"


def test_guidance_received_during_engine_shutdown_is_not_overwritten(forge):
    controller = start(forge)
    ModelFixture.blocked = True
    def answer_now():
        ModelFixture.blocked = False
        controller.command(3, Command('answer', 'Keep the existing interface'), 'maintainer', 'late-answer')
    ModelFixture.exit_control = answer_now
    _, status = run(forge, controller)
    assert status == 'complete'
    assert any('Keep the existing interface' in prompt for prompt in ModelFixture.prompts)
    assert len(forge.pulls) == 1


def test_stop_during_validation_prevents_publication(forge):
    controller = start(forge)
    runner = TaskRunner(forge, controller.store, 3, engine_factory=ModelFixture)
    def stop_now():
        controller.command(3, Command("stop"), "maintainer", "stop1")
        runner.fresh_state()
    ModelFixture.control = stop_now
    assert runner.run() == "stopped"
    assert not forge.closed and not forge.pulls
    assert controller.store.read(3)[0]['checkpoint']


def test_restart_keeps_old_branch_and_uses_new_one(forge):
    controller = start(forge)
    ModelFixture.blocked = True
    run(forge, controller)
    old = controller.store.read(3)[0]['branch']
    controller.command(3, Command("restart"), "maintainer", "restart1")
    run(forge, controller)
    new = controller.store.read(3)[0]['branch']
    assert old != new
    assert forge.branch_sha(forge.config.target_repository, old)
    assert forge.branch_sha(forge.config.target_repository, new)


def test_expired_lease_becomes_interrupted_with_recovery(forge):
    controller = start(forge)
    controller.store.claim(3, "lost")
    controller.store.mutate(3, lambda state: {**state, "lease_until": 1})
    controller.reconcile_expired(3)
    state = controller.store.read(3)[0]
    assert state['status'] == "interrupted" and state['desired'] == "paused"
    assert not state['runner'] and not forge.closed
    assert any("heartbeats" in text and "/banana resume" in text for text in forge.posts)


def test_public_issue_cannot_trigger_without_maintainer_permission(forge):
    controller = Controller(forge)
    event = {"action": "opened", "issue": {"number": 3, "id": 10}, "sender": {"login": "stranger"}}
    assert controller.event("issues", event) is None
    assert not forge.records and not forge.posts
    event['sender']['login'] = 'maintainer'
    event['issue']['pull_request'] = {'url': 'https://example.invalid'}
    assert controller.event("issues", event) is None


def test_configuration_and_command_changes_are_explicit(forge):
    controller = start(forge)
    controller.command(3, Command("model", "not-enabled"), "maintainer", "model1")
    assert controller.store.read(3)[0]['model'] == 'coding'
    controller.command(3, Command("model", "other"), "maintainer", "model2")
    assert controller.store.read(3)[0]['model'] == 'other'
    assert parse('please execute /banana start') is None
    assert parse('```\n/banana start\n```') is None
    assert parse('/banana answer Keep it compatible').argument == 'Keep it compatible'


def test_checkpoint_blocks_workflow_edits_and_git_metadata_is_not_exposed(forge, tmp_path):
    sha = forge.branch_sha(forge.config.target_repository, 'main')
    forge.create_branch(forge.config.target_repository, 'bananavibe/test', sha)
    workspace = Workspace(tmp_path / 'task', forge, 'bananavibe/test')
    workspace.prepare()
    assert not (workspace.path / '.git').exists()
    workflow = workspace.path / '.github/workflows/steal.yml'
    workflow.parent.mkdir(parents=True)
    workflow.write_text('malicious change')
    with pytest.raises(ValueError, match='protected'):
        workspace.checkpoint()
    assert forge.branch_sha(forge.config.target_repository, 'bananavibe/test') == sha


def test_example_configs_load_without_network():
    root = Path(__file__).resolve().parents[1]
    assert Config.load(root / 'examples/bananawiki.toml').target_repository == 'BananaSuite/BananaWiki'
    assert Config.load(root / 'examples/bananachat.toml').target_repository == 'BananaSuite/BananaChat'
