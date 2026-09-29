"""Command-line entry point.

    bananavibe [run] [--config FILE] [--event PATH --event-name NAME]
    bananavibe check-config [--config FILE]
    bananavibe migrate-state [--config FILE]   (one-off 2.x upgrade step)
    bananavibe backups ...        (see docs/backups.md)
    bananavibe restore-state ...  (see docs/backups.md)

`run` is the default and is what the Action invokes. The 2.x flag
`--check-config` still works.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from . import __version__
from .config import Config, ConfigurationError

OK_OUTCOMES = {"complete", "stopped", "busy", "blocked", "paused", "ignored", "lost"}


def _env(*names):
    return next((os.environ[name] for name in names if os.environ.get(name)), "")


def annotate(level, message):
    """Print a workflow annotation on Actions runners, plain text elsewhere."""
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("FORGEJO_ACTIONS") == "true":
        escaped = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level}::{escaped}", flush=True)
    else:
        print(f"{level}: {message}", file=sys.stderr, flush=True)


def _output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _load(path):
    config = Config.load(path, control_repository=_env("GITHUB_REPOSITORY", "FORGEJO_REPOSITORY") or None)
    for warning in config.warnings:
        annotate("warning", warning)
    return config


def check_config(path):
    config = _load(path)
    models = ", ".join(f"{alias} ({model.provider})" for alias, model in config.models.items())
    missing = sorted({m.key_env for m in config.models.values() if m.key_env and not os.environ.get(m.key_env)})
    print(f"Valid {config.forge} configuration: issues in {config.control_repository}, "
          f"changes to {config.target_repository}@{config.base_branch}.")
    print(f"Models: {models}; default {config.default_model}.")
    print(f"{len(config.prepare)} preparation command(s), {len(config.checks)} check(s); "
          f"egress: {', '.join(config.egress)}.")
    if missing:
        print(f"Not set in this environment (fine outside Actions): {', '.join(missing)}.")
    return 0


def migrate_state(path):
    from .forge import Forge
    from .state import StateStore

    config = _load(path)
    store = StateStore(Forge(config, os.environ.get("BANANAVIBE_TOKEN", "")))
    if store.make_orphan():
        print(f"Converted {config.state_branch} in {config.control_repository}: it now holds only task records.")
    else:
        print(f"{config.state_branch} already holds only task records; nothing to do.")
    return 0


def run(args):
    from .controller import Controller
    from .forge import Forge
    from .runner import TaskRunner

    config = _load(args.config)
    if not args.event or not args.event_name:
        raise ConfigurationError("No Actions event: pass --event and --event-name, or run inside a workflow.")
    event_path = Path(args.event)
    if event_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("The event payload exceeds 4 MiB.")
    payload = json.loads(event_path.read_text(encoding="utf-8"))
    forge = Forge(config, os.environ.get("BANANAVIBE_TOKEN", ""))
    print(f"BananaVibe {__version__}: {args.event_name} event for {config.control_repository} as {forge.login}.")
    controller = Controller(forge)
    number = controller.event(args.event_name, payload, _env("GITHUB_ACTOR", "FORGEJO_ACTOR"))
    if not number:
        _output("outcome", "ignored")
        return 0
    outcome = TaskRunner(forge, controller.store, number).run()
    _output("outcome", outcome)
    _output("issue", number)
    if outcome not in OK_OUTCOMES:
        annotate("error", f"Issue #{number}: the task {outcome}; see the issue for details.")
        return 1
    annotate("notice", f"Issue #{number}: task {outcome}.")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["backups"]:
        from .backups import main as backups
        return backups(argv[1:])
    if argv[:1] == ["restore-state"]:
        from .backups import replay_main
        return replay_main(argv[1:])
    parser = argparse.ArgumentParser(prog="bananavibe", description="Issue-driven maintenance drafts.")
    parser.add_argument("action", nargs="?", choices=("run", "check-config", "migrate-state", "version"), default="run")
    parser.add_argument("--config", default=os.environ.get("BANANAVIBE_CONFIG", ".bananavibe.toml"))
    parser.add_argument("--event", default=_env("GITHUB_EVENT_PATH", "FORGEJO_EVENT_PATH"))
    parser.add_argument("--event-name", default=_env("GITHUB_EVENT_NAME", "FORGEJO_EVENT_NAME"))
    parser.add_argument("--check-config", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "version":
            print(__version__)
            return 0
        if args.action == "check-config" or args.check_config:
            return check_config(args.config)
        if args.action == "migrate-state":
            return migrate_state(args.config)
        return run(args)
    except (ConfigurationError, ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        annotate("error", f"BananaVibe: {error}")
        return 1
