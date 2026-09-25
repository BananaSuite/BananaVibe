"""Run the Actions controller with a trusted configuration and event payload."""

import argparse
import json
import os
from pathlib import Path
import sys
import subprocess

from .config import Config
from .controller import Controller
from .forge import Forge
from .runner import TaskRunner


def _count(number, noun):
    """Return "<number> <noun>" with the noun agreeing with the number."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def main(argv=None):
    provided = list(sys.argv[1:] if argv is None else argv)
    if provided and provided[0] in {"backups", "restore-state"}:
        from . import backups
        return (backups.main if provided[0] == "backups" else backups.replay_main)(provided[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=".bananavibe.toml")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH", os.environ.get("FORGEJO_EVENT_PATH", "")))
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", os.environ.get("FORGEJO_EVENT_NAME", "")))
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config, control_repository=os.environ.get("GITHUB_REPOSITORY") or os.environ.get("FORGEJO_REPOSITORY"))
        if args.check_config:
            models = _count(len(config.models), "model")
            checks = _count(len(config.checks), "acceptance check")
            print(f"Valid {config.forge} configuration for {config.target_repository}; {models}, {checks}.")
            return 0
        if not args.event or not args.event_name:
            raise ValueError("Provide the Actions event path and event name.")
        event_path = Path(args.event)
        if event_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("The event payload exceeds 2 MiB.")
        event = json.loads(event_path.read_text())
        forge = Forge(config, os.environ.get("BANANAVIBE_TOKEN", ""))
        controller = Controller(forge)
        issue = controller.event(args.event_name, event, os.environ.get("GITHUB_ACTOR", os.environ.get("FORGEJO_ACTOR", "")))
        if issue:
            result = TaskRunner(forge, controller.store, issue).run()
            print("BananaVibe task state:", result)
            return 0 if result in {"complete", "stopped", "busy"} else 2
        print("BananaVibe event handled.")
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print("BananaVibe:", str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
