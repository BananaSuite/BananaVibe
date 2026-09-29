from bananavibe.commands import Command, parse
from bananavibe.controller import Controller


def comment_event(body, login="maintainer", number=3, comment_id=1):
    return {"action": "created", "issue": {"number": number}, "sender": {"login": login, "type": "User"},
            "comment": {"id": comment_id, "body": body}, "repository": {"full_name": "team/prompts"}}


def test_parse_accepts_only_whole_comment_commands():
    assert parse("/banana start") == Command("start")
    assert parse("  /BananaVibe STATUS \n") == Command("status")
    assert parse("/banana") == Command("help")
    assert parse("/banana answer Keep the API") == Command("answer", "Keep the API")
    assert parse("/banana answer\nFirst line\nSecond line") == Command("answer", "First line\nSecond line")
    assert parse("/banana revise tighten tests") == Command("answer", "tighten tests")
    assert parse("/banana model") == Command("help")
    assert parse("/banana start now") == Command("help")
    assert parse("/banana explode") == Command("help")
    for text in ("please /banana start", "```\n/banana start\n```", "/bananas start", "", None, "> /banana start"):
        assert parse(text) is None


def test_strangers_bots_and_pull_requests_cannot_start_work(forge):
    controller = Controller(forge, log=lambda *_: None)
    assert controller.event("issue_comment", comment_event("/banana start", login="stranger")) is None
    assert controller.event("issue_comment", {**comment_event("/banana start"), "sender": {"login": "maintainer",
                                                                                            "type": "Bot"}}) is None
    pull = comment_event("/banana start")
    pull["issue"]["pull_request"] = {"url": "x"}
    assert controller.event("issue_comment", pull) is None
    assert controller.event("issue_comment", comment_event("/banana start", login="bananavibe-bot")) is None
    assert forge.posts == [] and forge.branch_sha("team/prompts", "bananavibe-state") is None


def test_event_from_another_repository_is_rejected(forge):
    event = comment_event("/banana start")
    event["repository"]["full_name"] = "someone/else"
    controller = Controller(forge, log=lambda *_: None)
    try:
        controller.event("issue_comment", event)
    except ValueError as error:
        assert "different repository" in str(error)
    else:
        raise AssertionError("expected a repository mismatch")


def test_start_records_task_and_approval_then_deduplicates(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    assert controller.event("issue_comment", comment_event("/banana start")) == 3
    state, _ = store.read(3)
    assert state["status"] == "queued" and state["desired"] == "running" and state["approved"]
    assert controller.event("issue_comment", comment_event("/banana start")) is None  # same comment id


def test_opened_issue_starts_only_when_enabled(forge, store):
    event = {"action": "opened", "issue": {"number": 3, "id": 99}, "sender": {"login": "maintainer"}}
    assert Controller(forge, store, log=lambda *_: None).event("issues", event) == 3
    forge.config = forge.config.__class__(**{**forge.config.__dict__, "open_issues": False})
    forge.issues[4] = {"number": 4, "title": "x", "body": "y"}
    event["issue"] = {"number": 4, "id": 100}
    assert Controller(forge, store, log=lambda *_: None).event("issues", event) is None


def test_workflow_dispatch_uses_inputs(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    payload = {"inputs": {"issue": "#3", "command": "/banana start"}, "run_id": 5, "sender": {"login": "maintainer"}}
    assert controller.event("workflow_dispatch", payload, "maintainer") == 3


def test_commands_update_intent(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    controller.command(3, Command("start"), "maintainer", "e1")
    controller.command(3, Command("model", "missing"), "maintainer", "e2")
    assert store.read(3)[0]["model"] == "coding"
    assert "not a configured model" in forge.posts[-1]
    controller.command(3, Command("model", "other"), "maintainer", "e3")
    assert store.read(3)[0]["model"] == "other"
    assert controller.command(3, Command("stop"), "maintainer", "e4") is None
    assert store.read(3)[0]["status"] == "stopped"
    assert controller.command(3, Command("answer", "Keep the API"), "maintainer", "e5") == 3
    state = store.read(3)[0]
    assert state["guidance"][-1] == {"author": "maintainer", "text": "Keep the API"}
    assert state["desired"] == "running" and state["request"] == 4


def test_stop_while_running_waits_for_the_runner(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    controller.command(3, Command("start"), "maintainer", "e1")
    store.claim(3, "runner")
    controller.command(3, Command("stop"), "maintainer", "e2")
    state = store.read(3)[0]
    assert state["status"] == "stopping" and state["desired"] == "stopped" and state["runner"] == "runner"


def test_answer_on_a_complete_task_revises_and_reopens(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    controller.command(3, Command("start"), "maintainer", "e1")
    store.mutate(3, lambda state: {**state, "status": "complete", "desired": "complete", "pr_url": "u"})
    assert controller.command(3, Command("stop"), "maintainer", "e2") is None
    assert controller.command(3, Command("answer", "Rename the helper"), "maintainer", "e3") == 3
    state = store.read(3)[0]
    assert state["operation"] == "revise" and state["status"] == "queued"
    assert forge.issue_state[3] == "open"


def test_status_and_watchdog_reconcile_expired_leases(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    controller.command(3, Command("start"), "maintainer", "e1")
    store.claim(3, "lost-runner")
    store.mutate(3, lambda state: {**state, "lease_until": 1})
    controller.watchdog()
    state = store.read(3)[0]
    assert state["status"] == "interrupted" and state["desired"] == "paused" and not state["runner"]
    assert "stopped without finishing" in forge.posts[-1] and "/banana resume" in forge.posts[-1]


def test_help_models_and_unknown_task(forge, store):
    controller = Controller(forge, store, log=lambda *_: None)
    controller.command(3, Command("help"), "maintainer", "e1")
    assert "/banana start" in forge.posts[-1]
    controller.command(3, Command("models"), "maintainer", "e2")
    assert "`coding`" in forge.posts[-1] and "(default)" in forge.posts[-1]
    controller.command(3, Command("resume"), "maintainer", "e3")
    assert "no task" in forge.posts[-1]
    controller.command(3, Command("status"), "maintainer", "e4")
    assert "No task yet" in forge.posts[-1]
