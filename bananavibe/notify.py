"""Notifications: a shell command, a webhook, and a "dead man's switch" ping.

Everything runs in background threads with timeouts: a slow or broken notification channel can never block
or crash the supervisor. Call `flush()` before the process exits so the last messages (done, stopped,
crashed) are actually sent.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

from bananavibe.config import Config
from bananavibe.runner import kill_process_group
from bananavibe.util import Console, now

COMMAND_TIMEOUT = 60
HTTP_TIMEOUT = 20


class Notifier:
    def __init__(self, cfg: Config, workspace: str, console: Console | None = None):
        self.cfg = cfg
        self.workspace = workspace
        self.console = console
        self.host = socket.gethostname()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def enabled(self, event: str) -> bool:
        if not (self.cfg.notify_command or self.cfg.notify_url):
            return False
        return not self.cfg.notify_events or event in self.cfg.notify_events

    def throttled(self, key: str, minutes: float | None = None) -> bool:
        """True if a notification with this key was sent recently (repeat alerts are spaced out)."""
        interval = 60 * (self.cfg.notify_repeat_minutes if minutes is None else minutes)
        t = time.monotonic()
        with self._lock:
            last = self._last.get(key)
            if last is not None and t - last < interval:
                return True
            self._last[key] = t
        return False

    def reset(self, key: str) -> None:
        with self._lock:
            self._last.pop(key, None)

    def send(self, event: str, message: str, extra_env: dict[str, str] | None = None) -> None:
        if not self.enabled(event):
            return
        if self.cfg.notify_command:
            self._spawn(self._run_command, event, message, extra_env or {})
        if self.cfg.notify_url:
            self._spawn(self._post, event, message)

    def ping(self, suffix: str = "") -> None:
        """Tell an external monitor (healthchecks.io, Uptime Kuma, cronitor...) the run is alive."""
        if self.cfg.ping_url:
            self._spawn(self._ping, suffix)

    def _spawn(self, target, *args) -> None:
        t = threading.Thread(target=self._guarded, args=(target, *args), daemon=True, name="notify")
        with self._lock:
            self._threads = [x for x in self._threads if x.is_alive()]
            if len(self._threads) > 20:
                return  # a channel is hanging: don't pile up threads
            self._threads.append(t)
        t.start()

    def _guarded(self, target, *args) -> None:
        try:
            target(*args)
        except Exception as e:  # never let a notification problem escape
            self._warn(f"notification failed: {type(e).__name__}: {e}")

    def _warn(self, msg: str) -> None:
        if self.console:
            self.console.warn(msg)

    def _run_command(self, event: str, message: str, extra_env: dict[str, str]) -> None:
        env = {**os.environ, "BANANAVIBE_EVENT": event, "BANANAVIBE_MESSAGE": message,
               "BANANAVIBE_RUN": self.cfg.name, "BANANAVIBE_WORKSPACE": self.workspace,
               "BANANAVIBE_HOST": self.host, **extra_env}
        for key in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
            env.pop(key, None)
        try:
            proc = subprocess.Popen(["bash", "-c", self.cfg.notify_command], env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            self._warn(f"notify command failed to start: {e}")
            return
        try:
            _, err = proc.communicate(timeout=COMMAND_TIMEOUT)
            if proc.returncode:
                self._warn(f"notify command exited {proc.returncode}: {err.decode(errors='replace')[-300:].strip()}")
        except subprocess.TimeoutExpired:
            kill_process_group(proc.pid, grace=2)
            proc.wait()
            self._warn(f"notify command timed out after {COMMAND_TIMEOUT}s")

    def _post(self, event: str, message: str) -> None:
        title = f"BananaVibe {self.cfg.name}: {event}"
        fmt = self.cfg.notify_format
        headers = {"User-Agent": "bananavibe"}
        if fmt == "json":
            body = json.dumps({"event": event, "message": message, "run": self.cfg.name, "host": self.host,
                               "workspace": self.workspace, "at": now().isoformat(timespec="seconds")})
            headers["Content-Type"] = "application/json"
        elif fmt == "slack":
            body = json.dumps({"text": f"*{title}*\n{message}"})
            headers["Content-Type"] = "application/json"
        elif fmt == "discord":
            body = json.dumps({"content": f"**{title}**\n{message}"[:1990]})
            headers["Content-Type"] = "application/json"
        else:  # plain text, which ntfy.sh and most chat bridges accept; Title is ntfy's header
            body = message
            headers["Content-Type"] = "text/plain; charset=utf-8"
            headers["Title"] = title.encode("ascii", "replace").decode()
            if event in ("attention", "restart"):
                headers["Priority"] = "high"
        headers.update(self.cfg.notify_headers)
        req = urllib.request.Request(self.cfg.notify_url, data=body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                r.read(1024)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._warn(f"notify webhook failed: {e}")

    def _ping(self, suffix: str) -> None:
        url = self.cfg.ping_url.rstrip("/") + suffix if suffix else self.cfg.ping_url
        req = urllib.request.Request(url, data=b"", headers={"User-Agent": "bananavibe"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                r.read(256)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self._warn(f"ping failed: {e}")

    def flush(self, timeout: float = 30) -> None:
        """Wait (up to `timeout` seconds in total) for notifications still being sent."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
