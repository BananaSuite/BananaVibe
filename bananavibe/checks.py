"""Running the operator's verification commands."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from bananavibe.config import CheckConfig
from bananavibe.runner import agent_env, kill_group, kill_process_group
from bananavibe.util import clean, read_tail, tail


def run_check(check: CheckConfig, workspace: Path, log_dir: Path, stop: threading.Event | None = None,
              on_tick: Callable[[], None] | None = None) -> dict:
    cwd = (workspace / check.cwd).resolve()
    started = time.monotonic()
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in check.name) or "check"
    log_file = log_dir / f"check-{safe_name}.log"
    if not cwd.is_dir():
        return {"name": check.name, "ok": False, "exit": -1, "seconds": 0,
                "output": f"the check's directory {cwd} does not exist"}
    try:
        with log_file.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen(["bash", "-lc", check.run], cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                    env=agent_env({"CI": "1"}))
            try:
                deadline = started + check.timeout_minutes * 60
                note = ""
                while proc.poll() is None:
                    if on_tick:
                        on_tick()
                    if stop is not None and stop.is_set():
                        note = "[interrupted]"
                    elif time.monotonic() > deadline:
                        note = f"[timed out after {check.timeout_minutes:g} minutes]"
                    else:
                        time.sleep(0.5)
                        continue
                    kill_group(proc, grace=5)
                    break
                code = proc.wait() if not note else -1
            finally:
                if proc.poll() is None:
                    kill_group(proc, grace=5)
                kill_process_group(proc.pid, grace=2)  # servers the check started in the background
    except OSError as e:
        return {"name": check.name, "ok": False, "exit": -1, "seconds": 0, "output": str(e)}
    output = clean(read_tail(log_file, 64_000))
    output = tail(output, 6000)
    if note:
        output += "\n" + note
    return {"name": check.name, "ok": code == 0, "exit": code, "seconds": round(time.monotonic() - started, 1),
            "output": output}


def run_checks(checks: list[CheckConfig], workspace: Path, log_dir: Path,
               stop: threading.Event | None = None, on_tick: Callable[[], None] | None = None) -> list[dict]:
    results = []
    for c in checks:
        if stop is not None and stop.is_set():
            break
        results.append(run_check(c, workspace, log_dir, stop, on_tick))
    return results


def failure_text(results: list[dict]) -> str:
    parts = []
    for r in results:
        if not r["ok"]:
            parts.append(f"### Check `{r['name']}` failed (exit {r['exit']})\n\n```\n{tail(r['output'], 4000)}\n```")
    return "\n\n".join(parts)
