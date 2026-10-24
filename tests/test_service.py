"""Autostart (systemd / cron), notifications, periodic updates and the dead man's switch."""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import configure

from bananavibe import service
from bananavibe.config import load
from bananavibe.notify import Notifier
from bananavibe.state import Paths, State, supervisor_pid

ROOT = os.path.dirname(os.path.dirname(__file__))


def cli(ws: Path, *args: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "bananavibe", "-w", str(ws), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=120, env=env)


def wait_for(cond, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError("timed out waiting")


def test_systemd_unit_keeps_the_run_alive(workspace):
    paths = configure(workspace, ["plan"])
    cfg = load(paths.config, paths.workspace)
    unit = service.render_unit(paths, cfg, system=True)
    assert "$$" not in unit and "%%" not in unit
    for line in ("Restart=on-failure", "OOMPolicy=continue", "KillMode=mixed", "StartLimitIntervalSec=0",
                 "OOMScoreAdjust=-800", "WantedBy=multi-user.target", "NotifyAccess=all"):
        assert line in unit
    assert "run --service --daemon" in unit and str(paths.workspace) in unit
    user = service.render_unit(paths, cfg, system=False)
    assert "OOMScoreAdjust" not in user and "WantedBy=default.target" in user
    assert service.unit_name(paths, cfg).startswith("bananavibe-ws-")


@pytest.fixture
def fake_crontab(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "crontab.txt"
    script = bindir / "crontab"
    script.write_text(f"""#!/bin/sh
if [ "$1" = "-l" ]; then [ -f {store} ] && cat {store} || {{ echo "no crontab" >&2; exit 1; }}
elif [ "$1" = "-" ]; then cat > {store}
fi
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    store.write_text("0 3 * * * /usr/bin/backup\n")
    return store


def test_cron_autostart_install_is_idempotent_and_removable(workspace, fake_crontab):
    paths = configure(workspace, ["plan"])
    cfg = load(paths.config, paths.workspace)
    done = service.install(paths, cfg, "cron")
    assert done and service.installed(paths)["kind"] == "cron"
    text = fake_crontab.read_text()
    assert "/usr/bin/backup" in text and "@reboot" in text and "*/10 * * * *" in text
    assert service.install(paths, cfg, "cron") == []  # nothing to change
    assert fake_crontab.read_text() == text
    script = service.autostart_script_path(paths)
    assert not script.is_relative_to(paths.workspace)  # cron still finds it if .bananavibe/ is deleted
    assert "start --auto" in script.read_text() and str(paths.workspace) in script.read_text()
    service.uninstall(paths)
    assert fake_crontab.read_text().strip() == "0 3 * * * /usr/bin/backup"
    assert service.installed(paths) is None and not script.exists()


def test_start_sets_up_autostart(workspace, fake_crontab):
    paths = configure(workspace, ["hang"])
    paths.config.write_text(paths.config.read_text().replace("autostart = false", "autostart = true")
                            .replace("idle_timeout_minutes = 0.1", "idle_timeout_minutes = 5"))
    r = cli(workspace, "start", env={**os.environ, "BANANAVIBE_AUTOSTART_KIND": "cron"})
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        assert "crontab" in r.stdout and "@reboot" in fake_crontab.read_text()
        assert "cron" in cli(workspace, "status").stdout
    finally:
        cli(workspace, "stop", "--now", "--wait")
        service.uninstall(paths)


class _Hook(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        _Hook.received.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def webhook():
    _Hook.received = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", _Hook.received
    server.shutdown()


def test_webhook_and_command_notifications(workspace, webhook, monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    url, received = webhook
    out = workspace / "notified.txt"
    paths = configure(workspace, ["plan"], extra="")
    paths.config.write_text(paths.config.read_text() + f"""
[notify]
command = 'echo "$BANANAVIBE_EVENT|$BANANAVIBE_MESSAGE" >> {out}'
url = "{url}/hook"
format = "json"
headers = {{ Authorization = "Bearer t0ken" }}
""")
    cfg = load(paths.config, paths.workspace)
    n = Notifier(cfg, str(paths.workspace))
    n.send("done", "mission complete")
    n.flush(20)
    assert out.read_text().strip() == "done|mission complete"
    path, headers, body = received[0]
    assert path == "/hook" and headers["Authorization"] == "Bearer t0ken"
    assert json.loads(body)["event"] == "done" and json.loads(body)["message"] == "mission complete"
    cfg.notify_format = "text"
    cfg.notify_events = ["attention"]
    n.send("checkpoint", "filtered out")
    n.send("attention", "logged out")
    n.flush(20)
    assert received[-1][2] == "logged out" and "Title" in received[-1][1]
    assert len(received) == 2
    assert not n.throttled("x") and n.throttled("x") and not n.throttled("y")


def test_periodic_updates_and_pings_while_running(workspace, webhook, monkeypatch):
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    url, received = webhook
    paths = configure(workspace, ["sleep:6", "plan", "finish", "approve"])
    paths.config.write_text(paths.config.read_text().replace("idle_timeout_minutes = 0.1", "idle_timeout_minutes = 5")
                            + f"""
[notify]
url = "{url}/updates"
update_every_hours = 0.0005
ping_url = "{url}/ping"
ping_every_minutes = 0.02
""")
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    proc = subprocess.Popen([sys.executable, "-m", "bananavibe", "-w", str(workspace), "run"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert proc.wait(timeout=120) == 0
    finally:
        proc.kill()
    paths_hit = [r[0] for r in received]
    assert "/ping" in paths_hit
    updates = [r for r in received if r[0] == "/updates" and "update" in r[1].get("Title", "")]
    assert updates and "Running for" in updates[0][2]
    assert (paths.reports / "update-latest.md").exists()


def test_status_and_doctor_show_autostart_and_risks(workspace):
    paths = configure(workspace, ["plan"])
    State(paths).save()
    out = cli(workspace, "status").stdout
    assert "Autostart" in out
    doc = cli(workspace, "doctor").stdout
    assert "disk:" in doc and "autostart" in doc
    assert not supervisor_pid(Paths(workspace))


def test_systemd_unit_escapes_specials(tmp_path):
    ws = tmp_path / "my 100% $work"
    ws.mkdir()
    paths = Paths(ws)
    paths.root.mkdir()
    from bananavibe.config import parse
    unit = service.render_unit(paths, parse({}, ws), system=False)
    assert "WorkingDirectory=" + str(ws).replace("%", "%%") in unit
    exec_line = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert "100%%" in exec_line and "$$work" in exec_line


def test_unreadable_crontab_is_never_replaced(workspace, tmp_path, monkeypatch):
    bindir = tmp_path / "bin2"
    bindir.mkdir()
    (bindir / "crontab").write_text("#!/bin/sh\necho 'crontab: permission denied' >&2\nexit 1\n")
    (bindir / "crontab").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    paths = configure(workspace, ["plan"])
    with pytest.raises(service.ServiceError, match="cannot read the current crontab"):
        service.install(paths, load(paths.config, paths.workspace), "cron")
