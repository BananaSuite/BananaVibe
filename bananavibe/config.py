"""Loading and validating `.bananavibe/config.toml`."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KNOWN_TYPES = ("claude", "codex", "opencode", "custom")


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

    retry_initial_seconds: float = 30
    retry_max_seconds: float = 1800
    limit_fallback_seconds: float = 900
    auth_retry_seconds: float = 600

    git_snapshot: bool = True
    git_branch: str = "bananavibe/work"
    git_push: bool = False
    git_remote: str = "origin"
    git_repos: list[str] = field(default_factory=list)

    checks: list[CheckConfig] = field(default_factory=list)
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    notify_command: str = ""
    prompt_extra: str = ""
    allow_push: bool = False

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


def parse(data: dict, workspace: Path) -> Config:
    _check_unknown(data, {"run", "retry", "git", "checks", "agents", "notify", "prompt"}, "top level")
    cfg = Config(workspace=workspace)

    run = data.get("run", {})
    _check_unknown(run, {
        "name", "workers", "reviewer", "strategy", "session_timeout_minutes", "idle_timeout_minutes",
        "max_iterations", "max_hours", "checkpoint_every", "pause_at_checkpoint", "review_at_checkpoint",
        "done_approvals", "stall_limit", "resume_sessions", "asker", "ask_timeout_minutes",
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

    retry = data.get("retry", {})
    _check_unknown(retry, {"initial_seconds", "max_seconds", "limit_fallback_seconds", "auth_seconds"}, "retry")
    cfg.retry_initial_seconds = _get(retry, "initial_seconds", float, cfg.retry_initial_seconds)
    cfg.retry_max_seconds = _get(retry, "max_seconds", float, cfg.retry_max_seconds)
    cfg.limit_fallback_seconds = _get(retry, "limit_fallback_seconds", float, cfg.limit_fallback_seconds)
    cfg.auth_retry_seconds = _get(retry, "auth_seconds", float, cfg.auth_retry_seconds)

    git = data.get("git", {})
    _check_unknown(git, {"snapshot", "branch", "push", "remote", "repos"}, "git")
    cfg.git_snapshot = _get(git, "snapshot", bool, cfg.git_snapshot)
    cfg.git_branch = _get(git, "branch", str, cfg.git_branch)
    cfg.git_push = _get(git, "push", bool, cfg.git_push)
    cfg.git_remote = _get(git, "remote", str, cfg.git_remote)
    cfg.git_repos = _str_list(git, "repos", [])

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
    for name, a in agents.items():
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

    notify = data.get("notify", {})
    _check_unknown(notify, {"command"}, "notify")
    cfg.notify_command = _get(notify, "command", str, "")

    prompt = data.get("prompt", {})
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
    return cfg


def load(path: Path, workspace: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path} not found; run `bananavibe init` first") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    return parse(data, workspace)
