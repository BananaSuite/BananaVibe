"""Loading and validating `.bananavibe/config.toml`."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_TYPES = ("claude", "codex", "opencode", "custom")
NOTIFY_EVENTS = ("start", "checkpoint", "claim", "done", "stopped", "attention", "update", "restart")
NOTIFY_FORMATS = ("text", "json", "slack", "discord")
AGENT_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class ConfigError(Exception):
    pass


@dataclass
class AgentConfig:
    name: str
    type: str
    command: list[str]
    model: str = ""
    effort: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class CheckConfig:
    name: str
    run: str
    cwd: str = "."
    timeout_minutes: float = 30


@dataclass
class Config:
    workspace: Path
    name: str = "bananavibe"
    workers: list[str] = field(default_factory=lambda: ["claude"])
    reviewer: str = ""
    strategy: str = "failover"
    session_timeout_minutes: float = 120
    idle_timeout_minutes: float = 30
    max_iterations: int = 0
    max_hours: float = 0
    checkpoint_every: int = 5
    pause_at_checkpoint: bool = False
    review_at_checkpoint: bool = True
    done_approvals: int = 2
    stall_limit: int = 3
    resume_sessions: bool = True
    asker: str = ""
    ask_timeout_minutes: float = 15
    after_done: str = "wait"            # "wait" for new instructions (`bananavibe say`) and continue, or "exit"
    min_free_disk_mb: float = 1024      # below this, prune logs and wait a while before starting a session
    min_free_memory_mb: float = 256     # below this, wait a while before starting a session
    agent_oom_score_adj: int = 500      # Linux: make the OOM killer pick an agent (retried) over the supervisor
    watchdog_minutes: float = 60        # restart a supervisor whose main loop has been stuck this long (0 = off)
    autostart: bool = True              # `start` sets up starting at boot / when down (systemd or cron)

    retry_initial_seconds: float = 30
    retry_max_seconds: float = 1800
    limit_fallback_seconds: float = 900
    auth_retry_seconds: float = 600
    limit_recheck_seconds: float = 3600  # re-check a usage limit at least this often, even with a later reset time

    git_snapshot: bool = True
    git_branch: str = "bananavibe/work"
    git_push: bool = False
    git_remote: str = "origin"
    git_repos: list[str] = field(default_factory=list)
    git_max_file_mb: float = 50         # files larger than this are not snapshotted (0 = no limit)

    checks: list[CheckConfig] = field(default_factory=list)
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    notify_command: str = ""
    notify_url: str = ""
    notify_format: str = "text"
    notify_headers: dict[str, str] = field(default_factory=dict)
    notify_events: list[str] = field(default_factory=list)  # empty = all
    notify_repeat_minutes: float = 360  # repeated alerts about the same problem at most this often
    update_every_hours: float = 0       # periodic progress update (0 = off)
    ping_url: str = ""                  # dead man's switch (e.g. healthchecks.io), pinged while the run is healthy
    ping_every_minutes: float = 5
    prompt_extra: str = ""
    allow_push: bool = False

    logs_max_mb: float = 2048           # total size of logs/ before the oldest are deleted (0 = no limit)
    logs_compress_after: int = 20       # session logs older than the newest N are gzip-compressed (0 = never)

    @property
    def reviewer_name(self) -> str:
        return self.reviewer or self.workers[0]

    @property
    def asker_name(self) -> str:
        return self.asker or self.reviewer_name


def _default_agent(name: str, type_: str) -> AgentConfig:
    return AgentConfig(name=name, type=type_, command=[type_] if type_ != "custom" else [])


def _get(table: dict, key: str, kind: type | tuple, default):
    value = table.get(key, default)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if not isinstance(value, kind) or (kind in (int, float) and isinstance(value, bool)):
        raise ConfigError(f"'{key}' must be of type {getattr(kind, '__name__', kind)}, got {value!r}")
    return value


def _str_list(table: dict, key: str, default: list[str]) -> list[str]:
    value = table.get(key, default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{key}' must be a list of strings")
    return list(value)


def _check_unknown(table: dict, allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}")


def valid_branch(name: str) -> bool:
    """git's rules for branch names (git check-ref-format), without needing git."""
    if not name or name.startswith(("-", "/", ".")) or name.endswith(("/", ".", ".lock")) or name == "@":
        return False
    if any(bad in name for bad in ("..", "//", "@{", "/.")) or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", name):
        return False
    return True


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _str_table(table: dict, key: str, where: str) -> dict[str, str]:
    value = table.get(key, {})
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError(f"[{where}].{key} must be a table of strings")
    return dict(value)


def parse(data: dict, workspace: Path) -> Config:
    _check_unknown(data, {"run", "retry", "git", "checks", "agents", "notify", "prompt", "logs"}, "top level")
    cfg = Config(workspace=workspace)

    run = _section(data, "run")
    _check_unknown(run, {
        "name", "workers", "reviewer", "strategy", "session_timeout_minutes", "idle_timeout_minutes",
        "max_iterations", "max_hours", "checkpoint_every", "pause_at_checkpoint", "review_at_checkpoint",
        "done_approvals", "stall_limit", "resume_sessions", "asker", "ask_timeout_minutes", "after_done",
        "min_free_disk_mb", "min_free_memory_mb", "agent_oom_score_adj", "watchdog_minutes", "autostart",
    }, "run")
    cfg.name = _get(run, "name", str, workspace.resolve().name or "bananavibe")
    cfg.workers = _str_list(run, "workers", cfg.workers)
    cfg.reviewer = _get(run, "reviewer", str, "")
    cfg.strategy = _get(run, "strategy", str, cfg.strategy)
    cfg.session_timeout_minutes = _get(run, "session_timeout_minutes", float, cfg.session_timeout_minutes)
    cfg.idle_timeout_minutes = _get(run, "idle_timeout_minutes", float, cfg.idle_timeout_minutes)
    cfg.max_iterations = _get(run, "max_iterations", int, cfg.max_iterations)
    cfg.max_hours = _get(run, "max_hours", float, cfg.max_hours)
    cfg.checkpoint_every = _get(run, "checkpoint_every", int, cfg.checkpoint_every)
    cfg.pause_at_checkpoint = _get(run, "pause_at_checkpoint", bool, cfg.pause_at_checkpoint)
    cfg.review_at_checkpoint = _get(run, "review_at_checkpoint", bool, cfg.review_at_checkpoint)
    cfg.done_approvals = _get(run, "done_approvals", int, cfg.done_approvals)
    cfg.stall_limit = _get(run, "stall_limit", int, cfg.stall_limit)
    cfg.resume_sessions = _get(run, "resume_sessions", bool, cfg.resume_sessions)
    cfg.asker = _get(run, "asker", str, "")
    cfg.ask_timeout_minutes = _get(run, "ask_timeout_minutes", float, cfg.ask_timeout_minutes)
    cfg.after_done = _get(run, "after_done", str, cfg.after_done)
    cfg.min_free_disk_mb = _get(run, "min_free_disk_mb", float, cfg.min_free_disk_mb)
    cfg.min_free_memory_mb = _get(run, "min_free_memory_mb", float, cfg.min_free_memory_mb)
    cfg.agent_oom_score_adj = _get(run, "agent_oom_score_adj", int, cfg.agent_oom_score_adj)
    cfg.watchdog_minutes = _get(run, "watchdog_minutes", float, cfg.watchdog_minutes)
    cfg.autostart = _get(run, "autostart", bool, cfg.autostart)

    retry = _section(data, "retry")
    _check_unknown(retry, {"initial_seconds", "max_seconds", "limit_fallback_seconds", "auth_seconds",
                           "limit_recheck_seconds"}, "retry")
    cfg.retry_initial_seconds = _get(retry, "initial_seconds", float, cfg.retry_initial_seconds)
    cfg.retry_max_seconds = _get(retry, "max_seconds", float, cfg.retry_max_seconds)
    cfg.limit_fallback_seconds = _get(retry, "limit_fallback_seconds", float, cfg.limit_fallback_seconds)
    cfg.auth_retry_seconds = _get(retry, "auth_seconds", float, cfg.auth_retry_seconds)
    cfg.limit_recheck_seconds = _get(retry, "limit_recheck_seconds", float, cfg.limit_recheck_seconds)

    git = _section(data, "git")
    _check_unknown(git, {"snapshot", "branch", "push", "remote", "repos", "max_file_mb"}, "git")
    cfg.git_snapshot = _get(git, "snapshot", bool, cfg.git_snapshot)
    cfg.git_branch = _get(git, "branch", str, cfg.git_branch)
    cfg.git_push = _get(git, "push", bool, cfg.git_push)
    cfg.git_remote = _get(git, "remote", str, cfg.git_remote)
    cfg.git_repos = _str_list(git, "repos", [])
    cfg.git_max_file_mb = _get(git, "max_file_mb", float, cfg.git_max_file_mb)
    if cfg.git_branch and not valid_branch(cfg.git_branch):
        raise ConfigError(f"[git].branch {cfg.git_branch!r} is not a valid git branch name")

    checks = data.get("checks", [])
    if not isinstance(checks, list):
        raise ConfigError("[[checks]] must be an array of tables")
    for i, c in enumerate(checks):
        if not isinstance(c, dict):
            raise ConfigError("[[checks]] entries must be tables")
        _check_unknown(c, {"name", "run", "cwd", "timeout_minutes"}, f"checks #{i + 1}")
        if not c.get("run"):
            raise ConfigError(f"check #{i + 1} needs a 'run' command")
        cfg.checks.append(CheckConfig(
            name=_get(c, "name", str, f"check-{i + 1}"),
            run=_get(c, "run", str, ""),
            cwd=_get(c, "cwd", str, "."),
            timeout_minutes=_get(c, "timeout_minutes", float, 30.0),
        ))

    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise ConfigError("[agents] must be a table")
    for name, a in agents.items():
        if not AGENT_NAME.match(name):
            raise ConfigError(f"agent name {name!r} may only contain letters, digits, '_', '.' and '-'")
        if not isinstance(a, dict):
            raise ConfigError(f"[agents.{name}] must be a table")
        _check_unknown(a, {"type", "command", "model", "effort", "args", "env"}, f"agents.{name}")
        type_ = _get(a, "type", str, name if name in KNOWN_TYPES else "")
        if type_ not in KNOWN_TYPES:
            raise ConfigError(f"[agents.{name}] needs type = one of {', '.join(KNOWN_TYPES)}")
        agent = _default_agent(name, type_)
        if "command" in a:
            agent.command = _str_list(a, "command", [])
        agent.model = _get(a, "model", str, "")
        agent.effort = _get(a, "effort", str, "")
        agent.args = _str_list(a, "args", [])
        env = a.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise ConfigError(f"[agents.{name}].env must be a table of strings")
        agent.env = dict(env)
        if not agent.command:
            raise ConfigError(f"[agents.{name}] needs a 'command'")
        if type_ == "custom" and not any("{prompt}" in part or "{prompt_file}" in part for part in agent.command):
            raise ConfigError(f"[agents.{name}].command must contain {{prompt}} or {{prompt_file}}")
        cfg.agents[name] = agent

    for name in [*cfg.workers, cfg.reviewer_name, cfg.asker_name]:
        if name not in cfg.agents:
            if name in KNOWN_TYPES and name != "custom":
                cfg.agents[name] = _default_agent(name, name)
            else:
                raise ConfigError(f"agent '{name}' is used but not defined in [agents.{name}]")

    notify = _section(data, "notify")
    _check_unknown(notify, {"command", "url", "format", "headers", "events", "repeat_minutes", "update_every_hours",
                            "ping_url", "ping_every_minutes"}, "notify")
    cfg.notify_command = _get(notify, "command", str, "")
    cfg.notify_url = _get(notify, "url", str, "")
    cfg.notify_format = _get(notify, "format", str, cfg.notify_format)
    cfg.notify_headers = _str_table(notify, "headers", "notify")
    cfg.notify_events = _str_list(notify, "events", [])
    cfg.notify_repeat_minutes = _get(notify, "repeat_minutes", float, cfg.notify_repeat_minutes)
    cfg.update_every_hours = _get(notify, "update_every_hours", float, cfg.update_every_hours)
    cfg.ping_url = _get(notify, "ping_url", str, "")
    cfg.ping_every_minutes = _get(notify, "ping_every_minutes", float, cfg.ping_every_minutes)
    for url in (cfg.notify_url, cfg.ping_url):
        if url and not re.match(r"^https?://", url):
            raise ConfigError(f"[notify] URLs must start with http:// or https://, got {url!r}")
    if cfg.notify_format not in NOTIFY_FORMATS:
        raise ConfigError(f"[notify].format must be one of {', '.join(NOTIFY_FORMATS)}")
    unknown = set(cfg.notify_events) - set(NOTIFY_EVENTS)
    if unknown:
        raise ConfigError(f"[notify].events: unknown event(s) {', '.join(sorted(unknown))}; "
                          f"known: {', '.join(NOTIFY_EVENTS)}")

    logs = _section(data, "logs")
    _check_unknown(logs, {"max_mb", "compress_after"}, "logs")
    cfg.logs_max_mb = _get(logs, "max_mb", float, cfg.logs_max_mb)
    cfg.logs_compress_after = _get(logs, "compress_after", int, cfg.logs_compress_after)

    prompt = _section(data, "prompt")
    _check_unknown(prompt, {"extra", "allow_push"}, "prompt")
    cfg.prompt_extra = _get(prompt, "extra", str, "")
    cfg.allow_push = _get(prompt, "allow_push", bool, False)

    if not cfg.workers:
        raise ConfigError("[run].workers must name at least one agent")
    if cfg.strategy not in ("failover", "round-robin"):
        raise ConfigError("[run].strategy must be 'failover' or 'round-robin'")
    if cfg.session_timeout_minutes <= 0 or cfg.idle_timeout_minutes <= 0 or cfg.ask_timeout_minutes <= 0:
        raise ConfigError("timeouts must be positive")
    if cfg.done_approvals < 1:
        raise ConfigError("[run].done_approvals must be at least 1")
    if cfg.checkpoint_every < 0 or cfg.stall_limit < 1:
        raise ConfigError("[run].checkpoint_every must be >= 0 and stall_limit >= 1")
    if cfg.after_done not in ("exit", "wait"):
        raise ConfigError("[run].after_done must be 'exit' or 'wait'")
    if not -1000 <= cfg.agent_oom_score_adj <= 1000:
        raise ConfigError("[run].agent_oom_score_adj must be between -1000 and 1000")
    for key in ("retry_initial_seconds", "retry_max_seconds", "limit_fallback_seconds", "auth_retry_seconds"):
        if getattr(cfg, key) <= 0:
            raise ConfigError(f"[retry] values must be positive ({key.removeprefix('retry_')})")
    for key in ("limit_recheck_seconds", "min_free_disk_mb", "min_free_memory_mb", "watchdog_minutes",
                "git_max_file_mb", "update_every_hours", "logs_max_mb", "notify_repeat_minutes"):
        if getattr(cfg, key) < 0:
            raise ConfigError(f"{key} must not be negative")
    if cfg.ping_every_minutes <= 0 or cfg.logs_compress_after < 0:
        raise ConfigError("[notify].ping_every_minutes must be positive and [logs].compress_after >= 0")
    for c in cfg.checks:
        if c.timeout_minutes <= 0:
            raise ConfigError(f"check {c.name!r}: timeout_minutes must be positive")
    return cfg


def load(path: Path, workspace: Path) -> Config:
    try:
        return loads(path.read_text(encoding="utf-8"), workspace, str(path))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; run `bananavibe init` first") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from None


def loads(text: str, workspace: Path, where: str = "config.toml") -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{where}: {e}") from None
    return parse(data, workspace)
