import json
import subprocess
import sys
from pathlib import Path

import pytest

from bananavibe.cli import main
from bananavibe.config import load
from bananavibe.state import Paths
from bananavibe.supervisor import Supervisor
from bananavibe.util import Console

FAKE = Path(__file__).with_name("fake_agent.py")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True)
    subprocess.run(["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "init"], check=True)
    assert main(["-w", str(ws), "init", "--goal", "Create the files in the plan."]) == 0
    (ws / ".fake").mkdir()
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write("/.fake/\n")
    return ws


def configure(ws: Path, script: list[str], extra: str = "", workers: str = '["fake"]',
              reviewer: str = "fake", checks: str = "", checkpoint_every: int = 0) -> Paths:
    paths = Paths(ws)
    (ws / ".fake" / "script.json").write_text(json.dumps(script))
    command = json.dumps([sys.executable, str(FAKE), str(ws), "{prompt_file}"])
    paths.config.write_text(f"""
[run]
workers = {workers}
reviewer = "{reviewer}"
checkpoint_every = {checkpoint_every}
done_approvals = 1
stall_limit = 2
session_timeout_minutes = 2
idle_timeout_minutes = 0.1
{extra}

[retry]
initial_seconds = 0.2
max_seconds = 1
limit_fallback_seconds = 0.5
auth_seconds = 0.5

[agents.fake]
type = "custom"
command = {command}

[agents.fake2]
type = "custom"
command = {command}
{checks}
""")
    (ws / ".fake").mkdir(exist_ok=True)
    return paths


def run_supervisor(paths: Paths) -> tuple[int, Supervisor]:
    cfg = load(paths.config, paths.workspace)
    sup = Supervisor(paths, cfg, Console(paths.supervisor_log, quiet=True))
    return sup.run(), sup


def prompts(ws: Path) -> list[str]:
    return [p.read_text() for p in sorted((ws / ".fake").glob("prompt-*.md"))]
