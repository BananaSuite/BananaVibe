"""Adapters that run Claude Code, Codex, OpenCode or any other CLI agent headless.

Each adapter knows how to build the command line, how to turn the agent's
event stream into short human-readable lines, and how to decide whether a
session ended normally or hit a usage limit, an outage or a login problem.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bananavibe.config import AgentConfig

# Outcome kinds. Only "ok" counts as a finished session; everything else is retried.
OK = "ok"
LIMIT = "limit"          # usage or rate limit: wait for the reset, or fail over to another agent
TRANSIENT = "transient"  # overload, outage, network, crash: retry with backoff
AUTH = "auth"            # not logged in, bad key: retry slowly until a human fixes it
FATAL = "fatal"          # bad model name or flags: retry slowly, needs a human

_AUTH_RE = re.compile(
    r"not logged in|please (run )?/?login|log ?in again|invalid (x-)?api[ _-]?key|authentication[_ ]error"
    r"|unauthori[sz]ed|\b401\b|oauth token (has )?expired|token expired|credential",
    re.I,
)
_LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|\b429\b|too many requests|quota|hit your (usage )?limit|limit reached"
    r"|limit will reset|limit resets|weekly limit|session limit|credit balance is too low|out of credits",
    re.I,
)
_FATAL_RE = re.compile(
    r"model .{0,80}(not supported|not found|does not exist|is not available|unknown)|unknown model"
    r"|model unavailable|provider\.no-route|invalid_request_error"
    r"|invalid model|unknown (option|argument|flag)|unexpected argument|no such option|command not found"
    r"|no such file or directory",
    re.I,
)

UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
         "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400,
         "days": 86400}


def _unit_seconds(unit: str) -> int | None:
    return UNITS.get(unit)


def _next_clock_time(hour: int, minute: int, ref: dt.datetime) -> dt.datetime:
    t = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t <= ref:
        t += dt.timedelta(days=1)
    return t


def parse_retry_at(text: str, ref: dt.datetime | None = None) -> dt.datetime | None:
    """Find when a usage limit resets in an error message. Returns an aware datetime or None."""
    ref = ref or dt.datetime.now().astimezone()

    m = re.search(r"\|\s*(\d{10})\b", text) or re.search(r"reset(?:s|_at|sAt)?\"?\s*[:=]\s*\"?(\d{10})\b", text)
    if m:
        t = dt.datetime.fromtimestamp(int(m.group(1))).astimezone()
        return t if t > ref - dt.timedelta(minutes=5) else None

    m = re.search(r"(?:try again|retry|resets?|available again)\s+(?:in|after)\s+((?:\d+(?:\.\d+)?\s*[a-z]+[\s,]*(?:and\s+)?)+)",
                  text, re.I)
    if m:
        total = 0.0
        for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", m.group(1), re.I):
            secs = _unit_seconds(unit.lower())
            if secs is None:
                break
            total += float(num) * secs
        if total > 0:
            return ref + dt.timedelta(seconds=total)

    m = re.search(r"retry[- ]after\"?\s*[:=]?\s*(\d+)\b", text, re.I)
    if m:
        return ref + dt.timedelta(seconds=int(m.group(1)))

    m = re.search(
        r"(?:resets?|try again|available again)\s+(?:at\s+|on\s+)?"
        r"(?:(?P<mon>[A-Z][a-z]{2,8})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:(?P<year>\d{4}),?\s+)?(?:at\s+)?)?"
        r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
        r"(?:\s*\((?P<tz>[A-Za-z_]+/[A-Za-z_/]+|UTC)\))?",
        text, re.I,
    )
    if m and (m.group("ampm") or m.group("m")):
        hour = int(m.group("h"))
        minute = int(m.group("m") or 0)
        ampm = (m.group("ampm") or "").lower().replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        local_ref = ref
        if m.group("tz"):
            try:
                local_ref = ref.astimezone(ZoneInfo("UTC" if m.group("tz") == "UTC" else m.group("tz")))
            except (ZoneInfoNotFoundError, ValueError):
                pass
        if m.group("mon"):
            try:
                month = dt.datetime.strptime(m.group("mon")[:3], "%b").month
                year = int(m.group("year")) if m.group("year") else local_ref.year
                t = local_ref.replace(year=year, month=month, day=int(m.group("day")), hour=hour, minute=minute,
                                      second=0, microsecond=0)
                if not m.group("year") and t < local_ref - dt.timedelta(days=1):
                    t = t.replace(year=t.year + 1)
                return t.astimezone()
            except ValueError:
                return None
        return _next_clock_time(hour, minute, local_ref).astimezone()
    return None


def classify_error(text: str) -> tuple[str, dt.datetime | None]:
    """Classify an error message from an agent CLI or model API."""
    if not text.strip():
        return TRANSIENT, None
    if _AUTH_RE.search(text):
        return AUTH, None
    if _LIMIT_RE.search(text):
        return LIMIT, parse_retry_at(text)
    if _FATAL_RE.search(text):
        return FATAL, None
    return TRANSIENT, None


@dataclass
class Outcome:
    kind: str
    message: str = ""
    retry_at: dt.datetime | None = None
    final_text: str = ""
    usage: dict = field(default_factory=dict)  # cost_usd, tokens, windows (subscription utilization)


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class StreamParser:
    """Base parser for one session. Subclasses read the agent's JSON event stream."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.raw_tail: list[str] = []
        self.final_text = ""
        self.finished_ok = False
        self.usage: dict = {}

    def add_usage(self, cost: float | None = None, tokens: int = 0) -> None:
        if isinstance(cost, (int, float)):
            self.usage["cost_usd"] = self.usage.get("cost_usd", 0.0) + float(cost)
        if tokens:
            self.usage["tokens"] = self.usage.get("tokens", 0) + int(tokens)

    def finish(self, exit_code: int) -> Outcome:
        out = self.outcome(exit_code)
        out.usage = dict(self.usage)
        return out

    def feed(self, line: str) -> list[str]:
        """Consume one output line; return lines worth showing to a human."""
        self.raw_tail.append(line)
        if len(self.raw_tail) > 80:
            del self.raw_tail[:20]
        try:
            event = json.loads(line)
        except ValueError:
            return [_short(line)] if line.strip() else []
        if not isinstance(event, dict):
            return []
        return self.on_event(event)

    def on_event(self, event: dict) -> list[str]:
        return []

    def outcome(self, exit_code: int) -> Outcome:
        if exit_code == 0 and not self.errors:
            return Outcome(OK, final_text=self.final_text)
        text = "\n".join(self.errors) if self.errors else "\n".join(self.raw_tail[-30:])
        kind, retry_at = classify_error(text)
        if exit_code == 0 and self.finished_ok:
            return Outcome(OK, final_text=self.final_text)
        return Outcome(kind, _short(text, 600) or f"exit code {exit_code}", retry_at, self.final_text)


def _tool_hint(inp: dict) -> str:
    for key in ("command", "cmd", "file_path", "filePath", "path", "pattern", "url", "query", "description"):
        if isinstance(inp, dict) and inp.get(key):
            return _short(inp[key], 120)
    return ""


class ClaudeParser(StreamParser):
    def on_event(self, ev: dict) -> list[str]:
        t = ev.get("type")
        out: list[str] = []
        if t == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.final_text = block["text"]
                    out.append("💬 " + _short(block["text"]))
                elif block.get("type") == "tool_use":
                    out.append(f"🔧 {block.get('name')} {_tool_hint(block.get('input') or {})}")
        elif t == "result":
            u = ev.get("usage") or {}
            self.add_usage(ev.get("total_cost_usd"), sum(int(u.get(k) or 0) for k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
            text = str(ev.get("result") or "")
            if text:
                self.final_text = text
            if ev.get("is_error"):
                status = ev.get("api_error_status")
                self.errors.append(f"{text} {ev.get('subtype', '')} {'status ' + str(status) if status else ''}")
                out.append("✖ " + _short(text or str(ev.get("subtype"))))
            else:
                self.finished_ok = True
                cost = ev.get("total_cost_usd")
                out.append(f"✔ session finished: {ev.get('num_turns', '?')} turns"
                           + (f", ${cost:.2f} list price" if isinstance(cost, (int, float)) else ""))
        elif t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            self.rate_limit = info
            windows = info.get("unifiedWindows") or {}
            if windows:
                self.usage["windows"] = {k: v.get("utilization") for k, v in windows.items() if isinstance(v, dict)}
            if info.get("status") not in (None, "allowed", "allowed_warning"):
                out.append(f"⏳ rate limit {info.get('status')} ({info.get('rateLimitType', '?')})")
        elif t == "system" and ev.get("subtype") not in ("init", None):
            out.append(f"· {ev.get('subtype')}")
        return out

    rate_limit: dict | None = None

    def outcome(self, exit_code: int) -> Outcome:
        # A successful result event wins over noisy system events.
        if self.finished_ok and exit_code == 0:
            return Outcome(OK, final_text=self.final_text)
        result = super().outcome(exit_code)
        info = self.rate_limit or {}
        if info.get("status") not in (None, "allowed", "allowed_warning") and result.kind in (LIMIT, TRANSIENT):
            # Claude Code tells us exactly when the window resets.
            resets = info.get("resetsAt")
            result.kind = LIMIT
            if isinstance(resets, (int, float)):
                result.retry_at = dt.datetime.fromtimestamp(resets).astimezone()
        return result


class CodexParser(StreamParser):
    def on_event(self, ev: dict) -> list[str]:
        t = ev.get("type")
        if t in ("item.started", "item.completed"):
            item = ev.get("item") or {}
            kind = item.get("type") or item.get("item_type")
            if kind == "agent_message" and t == "item.completed":
                self.final_text = item.get("text", "")
                return ["💬 " + _short(self.final_text)]
            if kind == "command_execution" and t == "item.started":
                return ["🔧 $ " + _short(item.get("command", ""), 140)]
            if kind == "file_change" and t == "item.completed":
                paths = [c.get("path", "") for c in item.get("changes") or []]
                return ["✏️  " + _short(", ".join(paths), 140)]
            if kind in ("mcp_tool_call", "web_search") and t == "item.started":
                return [f"🔧 {kind} {_short(item.get('tool') or item.get('query') or '', 100)}"]
            if kind == "error" and t == "item.completed":
                return ["⚠ " + _short(item.get("message", ""))]
        elif t == "turn.completed":
            self.finished_ok = True
            u = ev.get("usage") or {}
            self.add_usage(None, int(u.get("input_tokens") or 0) + int(u.get("output_tokens") or 0))
            return ["✔ turn completed"]
        elif t == "turn.failed":
            self.errors.append(str((ev.get("error") or {}).get("message", ev)))
            return ["✖ " + _short(self.errors[-1])]
        elif t == "error":
            msg = str(ev.get("message", ""))
            # Codex reports its own reconnect attempts as errors; keep them, the exit code decides.
            self.errors.append(msg)
            return ["⚠ " + _short(msg)]
        return []

    def outcome(self, exit_code: int) -> Outcome:
        if self.finished_ok and exit_code == 0:
            return Outcome(OK, final_text=self.final_text)
        return super().outcome(exit_code)


class OpenCodeParser(StreamParser):
    def on_event(self, ev: dict) -> list[str]:
        t = ev.get("type")
        part = ev.get("part") or {}
        if t == "text" and part.get("text", "").strip():
            self.final_text = part["text"]
            return ["💬 " + _short(part["text"])]
        if t in ("tool_use", "tool"):
            state = part.get("state") or {}
            return [f"🔧 {part.get('tool', '?')} {_tool_hint(state.get('input') or {})}"]
        if t == "step_finish":
            self.finished_ok = True
            tok = part.get("tokens") or {}
            self.add_usage(part.get("cost"), int(tok.get("input") or 0) + int(tok.get("output") or 0))
            return []
        if t == "step_start":
            return []
        if t == "error" or ev.get("error"):
            err = ev.get("error") or part.get("error") or ev
            msg = json.dumps(err)[:800] if not isinstance(err, str) else err
            self.errors.append(msg)
            return ["✖ " + _short(msg)]
        return []

    def outcome(self, exit_code: int) -> Outcome:
        if exit_code == 0 and not self.errors and (self.finished_ok or self.final_text):
            return Outcome(OK, final_text=self.final_text)
        if exit_code == 0 and not self.errors:
            text = "\n".join(self.raw_tail[-30:])
            kind, retry_at = classify_error(text)
            return Outcome(kind, _short(text, 600) or "opencode produced no output", retry_at)
        return super().outcome(exit_code)


@dataclass
class Invocation:
    argv: list[str]
    stdin: str | None
    env: dict[str, str] = field(default_factory=dict)


class Adapter:
    parser_class = StreamParser

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    def base_env(self) -> dict[str, str]:
        return dict(self.cfg.env)

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path) -> Invocation:
        raise NotImplementedError

    def parser(self) -> StreamParser:
        return self.parser_class()


class ClaudeAdapter(Adapter):
    parser_class = ClaudeParser

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path) -> Invocation:
        argv = [*self.cfg.command, "-p", "--dangerously-skip-permissions",
                "--output-format", "stream-json", "--verbose"]
        if self.cfg.model:
            argv += ["--model", self.cfg.model]
        if self.cfg.effort:
            argv += ["--effort", self.cfg.effort]
        argv += self.cfg.args
        env = self.base_env()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # Claude Code refuses to skip permissions as root unless told it runs in a sandbox.
            env.setdefault("IS_SANDBOX", "1")
        return Invocation(argv, prompt, env)


class CodexAdapter(Adapter):
    parser_class = CodexParser

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path) -> Invocation:
        argv = [*self.cfg.command, "exec", "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check", "--json", "-C", str(cwd)]
        if self.cfg.model:
            argv += ["-m", self.cfg.model]
        if self.cfg.effort:
            argv += ["-c", f"model_reasoning_effort={self.cfg.effort}"]
        argv += [*self.cfg.args, "-"]
        return Invocation(argv, prompt, self.base_env())


class OpenCodeAdapter(Adapter):
    parser_class = OpenCodeParser

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path) -> Invocation:
        argv = [*self.cfg.command, "run", "--auto", "--format", "json"]
        model = self.cfg.model
        if model and self.cfg.effort and "#" not in model:
            model = f"{model}#{self.cfg.effort}"
        if model:
            argv += ["-m", model]
        argv += self.cfg.args
        # The full prompt lives in a file; the message points at it, so its size never hits argv limits.
        argv += ["--", f"Your complete instructions for this session are in the file {prompt_file}. "
                       "Read that whole file first and follow it exactly."]
        return Invocation(argv, None, self.base_env())


class CustomAdapter(Adapter):
    def invocation(self, prompt: str, prompt_file: Path, cwd: Path) -> Invocation:
        subs = {"{prompt}": prompt, "{prompt_file}": str(prompt_file), "{model}": self.cfg.model,
                "{effort}": self.cfg.effort, "{cwd}": str(cwd)}
        argv = []
        for part in [*self.cfg.command, *self.cfg.args]:
            for k, v in subs.items():
                part = part.replace(k, v)
            argv.append(part)
        return Invocation(argv, None, self.base_env())


ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter, "opencode": OpenCodeAdapter, "custom": CustomAdapter}


def make_adapter(cfg: AgentConfig) -> Adapter:
    return ADAPTERS[cfg.type](cfg)
