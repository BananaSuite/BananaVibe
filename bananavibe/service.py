"""Starting the run automatically: at boot, and whenever it is not running but should be.

`bananavibe start` installs this once per workspace (unless `[run] autostart = false`):

- **systemd, as root**: a system unit (`/etc/systemd/system/bananavibe-<name>-<id>.service`) that starts at
  boot and is restarted if it ever fails. `OOMPolicy=continue` keeps the service up when the kernel kills an
  agent for memory (systemd's default would stop the whole service), and `KillMode=mixed` lets the supervisor
  interrupt the agent cleanly on shutdown, so the session is continued after the reboot.
- **systemd, as a normal user** with lingering enabled (so it runs without a login session): the same as a
  user unit.
- **otherwise, cron**: an `@reboot` entry plus a check every 10 minutes that starts the run if it is down
  (and was not stopped by the operator).

Inside all of these, `bananavibe run` is the guardian that restarts a crashed, killed or hung supervisor.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from bananavibe.config import Config
from bananavibe.state import Paths
from bananavibe.util import read_json, read_text, write_atomic, write_json

CRON_TAG = "# bananavibe-autostart:"


class ServiceError(Exception):
    pass


def unit_name(paths: Paths, cfg: Config) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", cfg.name).strip("-")[:40] or "run"
    digest = hashlib.sha1(str(paths.workspace).encode()).hexdigest()[:6]
    return f"bananavibe-{slug}-{digest}"


def source_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def base_argv(paths: Paths) -> list[str]:
    return [sys.executable, "-P", "-m", "bananavibe", "-w", str(paths.workspace)]


def _run(argv: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 1, "", str(e))


def installed(paths: Paths) -> dict | None:
    info = read_json(paths.root / "service.json", None)
    return info if isinstance(info, dict) and info.get("kind") else None


def has_systemd() -> bool:
    return bool(shutil.which("systemctl")) and Path("/run/systemd/system").is_dir()


def linger_enabled(user: str) -> bool:
    if Path(f"/var/lib/systemd/linger/{user}").exists():
        return True
    r = _run(["loginctl", "show-user", user, "-p", "Linger", "--value"])
    return r.returncode == 0 and r.stdout.strip() == "yes"


def user_manager_ok() -> bool:
    r = _run(["systemctl", "--user", "show-environment"])
    return r.returncode == 0


def kill_user_processes() -> bool | None:
    """Whether logind kills a user's processes at logout (None if unknown)."""
    for conf in [Path("/etc/systemd/logind.conf"), *sorted(Path("/etc/systemd/logind.conf.d").glob("*.conf"))]:
        for line in read_text(conf).splitlines():
            m = re.match(r"^\s*KillUserProcesses\s*=\s*(\S+)", line)
            if m:
                return m.group(1).lower() in ("yes", "true", "1", "on")
    return None if has_systemd() else False


KINDS = ("systemd-system", "systemd-user", "cron")


def detect(user: str | None = None, enable_linger: bool = False) -> str | None:
    """The best autostart mechanism available here: systemd-system, systemd-user, cron or None.

    BANANAVIBE_AUTOSTART_KIND forces one (or "none"). Only with `enable_linger` does it try to turn on
    lingering for the user (which a user service needs); otherwise it just looks.
    """
    forced = os.environ.get("BANANAVIBE_AUTOSTART_KIND", "").strip()
    if forced:
        return forced if forced in KINDS else None
    if has_systemd():
        if os.geteuid() == 0:
            return "systemd-system"
        user = user or getpass.getuser()
        if user_manager_ok():
            if linger_enabled(user):
                return "systemd-user"
            if enable_linger and _run(["loginctl", "enable-linger", user]).returncode == 0 \
                    and linger_enabled(user):
                return "systemd-user"
    if shutil.which("crontab"):
        return "cron"
    return None


def state_base(paths: Paths) -> str:
    """BANANAVIBE_STATE_DIR as this process resolved it: pinned in the service, so that the backup and the
    outside lock are the same ones whatever environment cron or systemd start it with."""
    return str(paths.machine.parent.parent) if paths.machine else ""


def render_unit(paths: Paths, cfg: Config, system: bool) -> str:
    argv = [*base_argv(paths), "run", "--service", "--daemon"]
    env_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    ws = _systemd_escape(str(paths.workspace))
    lines = [
        "# Managed by `bananavibe start` / `bananavibe service`. Remove with `bananavibe service uninstall`.",
        "[Unit]",
        f"Description=BananaVibe run {_systemd_escape(cfg.name)} ({ws})",
        "After=network-online.target",
        "Wants=network-online.target",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={ws}",  # a plain path (spaces are fine, quotes are not)
        "ExecStart=" + " ".join(_systemd_quote(a, exec_line=True) for a in argv),
        f"Environment={_systemd_quote('PATH=' + env_path)}",
        f"Environment={_systemd_quote('PYTHONPATH=' + source_root())}",
        "Environment=PYTHONUNBUFFERED=1",
        *([f"Environment={_systemd_quote('BANANAVIBE_STATE_DIR=' + state_base(paths))}"] if state_base(paths) else []),
        f"EnvironmentFile=-{_systemd_escape(str(paths.root / 'service.env'))}",
        # A clean exit (finished, or stopped with `bananavibe stop`) is final; anything else, including being
        # killed by a stray signal, is restarted. `systemctl stop` stops it until the next boot.
        "Restart=on-failure",
        "RestartSec=20",
        "KillMode=mixed",
        "KillSignal=SIGTERM",
        "TimeoutStopSec=180",
        "OOMPolicy=continue",
        "NotifyAccess=all",
    ]
    if system:
        lines += [f"User={getpass.getuser() if os.geteuid() != 0 else 'root'}", "OOMScoreAdjust=-800"]
    lines += ["", "[Install]", f"WantedBy={'multi-user.target' if system else 'default.target'}", ""]
    return "\n".join(lines)


def _systemd_escape(value: str) -> str:
    """systemd expands %-specifiers in unit files: a literal % must be doubled."""
    return value.replace("%", "%%")


def _systemd_quote(arg: str, exec_line: bool = False) -> str:
    value = _systemd_escape(arg)
    if exec_line:
        value = value.replace("$", "$$")  # ExecStart also expands $VARIABLES
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _service_env_template(paths: Paths) -> None:
    env = paths.root / "service.env"
    if not env.exists():
        write_atomic(env, "# Extra environment for the background service (KEY=value per line), e.g. API keys\n"
                          "# your agents need that are normally set by your shell profile.\n")
        os.chmod(env, 0o600)


def autostart_script_path(paths: Paths) -> Path:
    # Outside the workspace when possible: cron must still find it if `.bananavibe/` was deleted (the run is
    # then rebuilt from its backup by `start --auto`).
    return (paths.machine or paths.root) / "autostart.sh"


def _autostart_script(paths: Paths) -> Path:
    script = autostart_script_path(paths)
    text = ("#!/bin/sh\n"
            "# Started by cron (see `crontab -l`): starts the run if it is down. Managed by `bananavibe service`.\n"
            f"cd {shlex.quote(str(paths.workspace))} || exit 1\n"
            f"mkdir -p {shlex.quote(str(paths.logs))} || exit 1\n"
            f"PATH={shlex.quote(os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'))}; export PATH\n"
            f"PYTHONPATH={shlex.quote(source_root())}; export PYTHONPATH\n"
            + (f"BANANAVIBE_STATE_DIR={shlex.quote(state_base(paths))}; export BANANAVIBE_STATE_DIR\n"
               if state_base(paths) else "")
            + f"[ -f {shlex.quote(str(paths.root / 'service.env'))} ] && "
            f"set -a && . {shlex.quote(str(paths.root / 'service.env'))} && set +a\n"
            f"exec {shlex.join(base_argv(paths))} start --auto >> "
            f"{shlex.quote(str(paths.logs / 'autostart.log'))} 2>&1\n")
    if read_text(script) != text:
        write_atomic(script, text)
    os.chmod(script, 0o700)
    return script


def _crontab_lines() -> list[str]:
    r = _run(["crontab", "-l"])
    if r.returncode == 0:
        return r.stdout.splitlines()
    if "no crontab" in (r.stderr + r.stdout).lower():
        return []
    # Anything else (crontab not readable, a timeout...): never risk replacing the user's crontab.
    raise ServiceError(f"cannot read the current crontab: {(r.stderr or r.stdout).strip() or 'unknown error'}")


def _write_crontab(lines: list[str]) -> None:
    text = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
    try:
        r = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ServiceError(f"cannot update the crontab: {e}") from None
    if r.returncode != 0:
        raise ServiceError(f"cannot update the crontab: {r.stderr.strip()}")


def install(paths: Paths, cfg: Config, kind: str | None = None) -> list[str]:
    """Install (or refresh) autostart for this workspace. Returns what was done."""
    kind = kind or detect(enable_linger=True)
    if not kind:
        raise ServiceError("neither systemd nor cron is available: the run restarts itself after crashes, but not "
                           "after a reboot")
    paths.logs.mkdir(parents=True, exist_ok=True)
    _service_env_template(paths)
    name = unit_name(paths, cfg)
    done: list[str] = []
    old = installed(paths)
    if old and old.get("kind") != kind:
        done += uninstall(paths)
    if kind in ("systemd-system", "systemd-user"):
        system = kind == "systemd-system"
        if system and os.geteuid() != 0:
            raise ServiceError("installing a system service needs root (run it with sudo, or use the user service)")
        unit_dir = Path("/etc/systemd/system") if system else Path.home() / ".config" / "systemd" / "user"
        unit_path = unit_dir / f"{name}.service"
        text = render_unit(paths, cfg, system)
        ctl = ["systemctl"] if system else ["systemctl", "--user"]
        changed = read_text(unit_path) != text
        if changed:
            unit_dir.mkdir(parents=True, exist_ok=True)
            write_atomic(unit_path, text)
            os.chmod(unit_path, 0o644)
            r = _run([*ctl, "daemon-reload"])
            if r.returncode != 0:
                raise ServiceError(f"systemctl daemon-reload failed: {r.stderr.strip()}")
        r = _run([*ctl, "enable", f"{name}.service"])
        if r.returncode != 0:
            raise ServiceError(f"systemctl enable failed: {r.stderr.strip()}")
        if changed:
            done.append(f"installed the {'system' if system else 'user'} service {name} ({unit_path}): it starts at "
                        "boot and restarts after failures")
        write_json(paths.root / "service.json", {"kind": kind, "unit": name, "path": str(unit_path)})
    elif kind == "cron":
        script = _autostart_script(paths)
        tag = f"{CRON_TAG}{name}"
        command = shlex.quote(str(script)).replace("%", "\\%")  # % means newline to cron
        entries = [f"@reboot {command} {tag}", f"*/10 * * * * {command} {tag}"]
        lines = _crontab_lines()
        kept = [ln for ln in lines if not ln.rstrip().endswith(tag)]
        if kept + entries != lines:
            _write_crontab(kept + entries)
            done.append(f"added crontab entries: start at boot and every 10 minutes if the run is down ({script})")
        write_json(paths.root / "service.json", {"kind": kind, "unit": name, "path": str(script)})
    else:
        raise ServiceError(f"unknown autostart kind {kind!r}")
    return done


def refresh_cron(paths: Paths, cfg: Config) -> None:
    """Bring a cron autostart set up by an older version up to date (script location and contents)."""
    info = installed(paths)
    if not info or info["kind"] != "cron":
        return
    if info.get("path") != str(autostart_script_path(paths)):
        install(paths, cfg, "cron")
    else:
        _autostart_script(paths)


def uninstall(paths: Paths) -> list[str]:
    info = installed(paths)
    if not info:
        return []
    done = []
    kind, name = info["kind"], info.get("unit", "")
    if kind.startswith("systemd"):
        ctl = ["systemctl"] if kind == "systemd-system" else ["systemctl", "--user"]
        _run([*ctl, "disable", f"{name}.service"])
        unit = Path(info.get("path", ""))
        if unit.is_file():
            try:
                unit.unlink()
            except OSError as e:
                raise ServiceError(f"cannot remove {unit}: {e}") from None
        _run([*ctl, "daemon-reload"])
        done.append(f"removed the service {name} (a running supervisor keeps running; `bananavibe stop` stops it)")
    elif kind == "cron":
        tag = f"{CRON_TAG}{name}"
        lines = _crontab_lines()
        kept = [ln for ln in lines if not ln.rstrip().endswith(tag)]
        if kept != lines:
            _write_crontab(kept)
        if info.get("path"):
            Path(info["path"]).unlink(missing_ok=True)
        done.append("removed the crontab entries")
    (paths.root / "service.json").unlink(missing_ok=True)
    return done


def start(paths: Paths, info: dict) -> tuple[bool, str]:
    """Start the run through its service manager. Returns (ok, message)."""
    if info["kind"].startswith("systemd"):
        ctl = ["systemctl"] if info["kind"] == "systemd-system" else ["systemctl", "--user"]
        r = _run([*ctl, "start", f"{info['unit']}.service"], timeout=90)
        if r.returncode != 0:
            return False, f"systemctl start failed: {(r.stderr or r.stdout).strip()}"
        return True, f"started the service {info['unit']}"
    return False, ""


def describe(info: dict | None) -> str:
    if not info:
        return "none (it restarts itself after crashes, but not after a reboot)"
    kind = info["kind"]
    if kind == "systemd-system":
        return f"systemd system service {info['unit']} (starts at boot)"
    if kind == "systemd-user":
        return f"systemd user service {info['unit']} (starts at boot, lingering)"
    return "cron (@reboot, and every 10 minutes if down)"


def status_text(info: dict | None) -> str:
    if not info or not info["kind"].startswith("systemd"):
        return describe(info)
    ctl = ["systemctl"] if info["kind"] == "systemd-system" else ["systemctl", "--user"]
    r = _run([*ctl, "is-active", f"{info['unit']}.service"])
    return f"{describe(info)}: {r.stdout.strip() or 'unknown'}"
