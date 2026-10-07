"""Running the operator's verification commands."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from bananavibe.config import CheckConfig
from bananavibe.runner import kill_group
from bananavibe.util import tail


def run_check(check: CheckConfig, workspace: Path, log_dir: Path, stop: threading.Event | None = None) -> dict:
    cwd = (workspace / check.cwd).resolve()
    started = time.monotonic()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"check-{check.name}.log"
    try:
        with log_file.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen(["bash", "-lc", check.run], cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                    env={**os.environ, "CI": "1"})
            deadline = started + check.timeout_minutes * 60
            note = ""
            while proc.poll() is None:
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
    except OSError as e:
        return {"name": check.name, "ok": False, "exit": -1, "seconds": 0, "output": str(e)}
    output = tail(log_file.read_text(encoding="utf-8", errors="replace"), 6000)
    if note:
        output += "\n" + note
    return {"name": check.name, "ok": code == 0, "exit": code, "seconds": round(time.monotonic() - started, 1),
            "output": output}


def run_checks(checks: list[CheckConfig], workspace: Path, log_dir: Path,
               stop: threading.Event | None = None) -> list[dict]:
    results = []
    for c in checks:
        if stop is not None and stop.is_set():
            break
        results.append(run_check(c, workspace, log_dir, stop))
    return results


def failure_text(results: list[dict]) -> str:
    parts = []
    for r in results:
        if not r["ok"]:
            parts.append(f"### Check `{r['name']}` failed (exit {r['exit']})\n\n```\n{tail(r['output'], 4000)}\n```")
    return "\n\n".join(parts)
