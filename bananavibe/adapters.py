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
from bananavibe.util import clean

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
    if not (m and (m.group("ampm") or m.group("m"))):
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ampm = (m.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    zone = None
    if m.group("tz"):
        try:
            zone = ZoneInfo("UTC" if m.group("tz").upper() == "UTC" else m.group("tz"))
        except (ZoneInfoNotFoundError, ValueError):
            zone = None

    def at(day: dt.date) -> dt.datetime:
        # Build the wall-clock time in its own zone, so daylight saving time on that date is respected
        # (carrying today's UTC offset to a date after a DST change would be an hour off).
        naive = dt.datetime(day.year, day.month, day.day, hour, minute)
        return naive.replace(tzinfo=zone) if zone else naive.astimezone()

    local_ref = ref.astimezone(zone) if zone else ref.astimezone()
    try:
        if m.group("mon"):
            month = dt.datetime.strptime(m.group("mon")[:3], "%b").month
            year = int(m.group("year")) if m.group("year") else local_ref.year
            t = at(dt.date(year, month, int(m.group("day"))))
            if not m.group("year") and t < ref - dt.timedelta(days=1):
                t = at(dt.date(year + 1, month, int(m.group("day"))))
            return t.astimezone()
        t = at(local_ref.date())
        if t <= ref:
            t = at(local_ref.date() + dt.timedelta(days=1))
        return t.astimezone()
    except ValueError:
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
    retry_exact: bool = False                  # retry_at comes from the agent's structured data, not a message


def _short(text: str, n: int = 160) -> str:
    text = " ".join(clean(str(text)).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class StreamParser:
    """Base parser for one session. Subclasses read the agent's JSON event stream."""

    plain_output = True  # plain-text agents: their output is their final message

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.raw_tail: list[str] = []
        self.text_tail: list[str] = []  # lines that are not JSON events: the CLI's own messages and stderr
        self.final_text = ""
        self.finished_ok = False
        self.usage: dict = {}
        self.session_id = ""  # the agent's own conversation id, for `bananavibe open` and resuming
        self.plain: list[str] = []

    def add_usage(self, cost: float | None = None, tokens: int = 0) -> None:
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.usage["cost_usd"] = self.usage.get("cost_usd", 0.0) + float(cost)
        if tokens:
            self.usage["tokens"] = self.usage.get("tokens", 0) + int(tokens)

    def finish(self, exit_code: int) -> Outcome:
        if self.plain_output and not self.final_text:
            self.final_text = clean("\n".join(self.plain[-200:])).strip()
        out = self.outcome(exit_code)
        out.usage = dict(self.usage)
        return out

    @staticmethod
    def _keep(buf: list[str], line: str, n: int) -> None:
        buf.append(line[:4000])
        if len(buf) > n + n // 4:
            del buf[: len(buf) - n]

    def feed(self, line: str) -> list[str]:
        """Consume one output line; return lines worth showing to a human."""
        self._keep(self.raw_tail, line, 80)
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            # Plain text (or a bare number/word that happens to be valid JSON): the CLI's own output.
            self._keep(self.text_tail, line, 60)
            if self.plain_output:
                self._keep(self.plain, line, 400)
            return [_short(line)] if line.strip() else []
        if not self.session_id:
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            sid = (event.get("session_id") or event.get("sessionID") or part.get("sessionID")
                   or (event.get("thread_id") if event.get("type") == "thread.started" else None))
            if isinstance(sid, str) and sid:
                self.session_id = sid
        try:
            return self.on_event(event)
        except (AttributeError, TypeError, ValueError, KeyError):
            # An event in a shape we don't know (a newer CLI version): never let it break the session.
            return []

    def on_event(self, event: dict) -> list[str]:
        return []

    def error_text(self) -> str:
        """The text to classify when a session failed: explicit errors, else what the CLI printed itself.

        The JSON event stream is deliberately left out for agents that have one: it contains the project's
        own output (tests about rate limiting, "401" in an API's logs...), which must not be mistaken for the
        agent's state.
        """
        if self.errors:
            return "\n".join(self.errors)
        if self.plain_output:
            return "\n".join(self.raw_tail[-30:])
        return "\n".join(self.text_tail[-30:])

    def outcome(self, exit_code: int) -> Outcome:
        if exit_code == 0 and not self.errors:
            return Outcome(OK, final_text=self.final_text)
        if exit_code == 0 and self.finished_ok:
            return Outcome(OK, final_text=self.final_text)
        text = self.error_text()
        kind, retry_at = classify_error(text)
        return Outcome(kind, _short(text, 600) or f"exit code {exit_code}", retry_at, self.final_text)


def _tool_hint(inp: dict) -> str:
    for key in ("command", "cmd", "file_path", "filePath", "path", "pattern", "url", "query", "description"):
        if isinstance(inp, dict) and inp.get(key):
            return _short(inp[key], 120)
    return ""


_API_ERROR_TEXT = re.compile(r"^\s*(API Error\b|Claude AI usage limit reached|.{0,40}usage limit reached\|\d{10})", re.I)


class ClaudeParser(StreamParser):
    plain_output = False

    def on_event(self, ev: dict) -> list[str]:
        t = ev.get("type")
        out: list[str] = []
        if t == "assistant":
            api_error = bool(ev.get("error") or ev.get("isApiErrorMessage"))
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    if api_error or _API_ERROR_TEXT.match(block["text"]):
                        # Claude Code reports API failures (limits, overload, logout) as a synthetic message.
                        self.errors.append(block["text"])
                        out.append("✖ " + _short(block["text"]))
                        continue
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
            if isinstance(resets, (int, float)) and not isinstance(resets, bool):
                result.retry_at = dt.datetime.fromtimestamp(resets).astimezone()
                result.retry_exact = True
        return result


class CodexParser(StreamParser):
    plain_output = False

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
                return ["✏ " + _short(", ".join(paths), 140)]
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
    plain_output = False

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
    supports_resume = False     # can continue an interrupted conversation by its session id
    enforces_read_only = False  # `read_only` is enforced by the agent itself, not just asked for in the prompt

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    def base_env(self) -> dict[str, str]:
        return dict(self.cfg.env)

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path, *, read_only: bool = False,
                   resume_id: str | None = None) -> Invocation:
        raise NotImplementedError

    def open_command(self, session_id: str, cwd: Path) -> list[str] | None:
        """Command that opens a recorded session in the agent's own interactive UI."""
        return None

    def parser(self) -> StreamParser:
        return self.parser_class()


# Tools a read-only `bananavibe ask` session may use: reading files and inspecting git history.
READ_ONLY_CLAUDE_TOOLS = ["Read", "Grep", "Glob", "LS", "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)",
                          "Bash(git status:*)", "Bash(git branch:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)",
                          "Bash(tail:*)", "Bash(wc:*)", "Bash(grep:*)"]


class ClaudeAdapter(Adapter):
    parser_class = ClaudeParser
    supports_resume = True
    enforces_read_only = True

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path, *, read_only: bool = False,
                   resume_id: str | None = None) -> Invocation:
        argv = [*self.cfg.command, "-p", "--output-format", "stream-json", "--verbose"]
        if read_only:
            # No permission bypass: in -p mode anything not allowed here is denied.
            argv += ["--allowedTools", ",".join(READ_ONLY_CLAUDE_TOOLS),
                     "--disallowedTools", "Edit,Write,NotebookEdit"]
        else:
            argv.insert(len(self.cfg.command) + 1, "--dangerously-skip-permissions")
        if resume_id:
            argv += ["--resume", resume_id]
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

    def open_command(self, session_id: str, cwd: Path) -> list[str] | None:
        return [*self.cfg.command, "--resume", session_id]


class CodexAdapter(Adapter):
    parser_class = CodexParser
    enforces_read_only = True

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path, *, read_only: bool = False,
                   resume_id: str | None = None) -> Invocation:
        mode = ["--sandbox", "read-only"] if read_only else ["--dangerously-bypass-approvals-and-sandbox"]
        argv = [*self.cfg.command, "exec", *mode, "--skip-git-repo-check", "--json", "-C", str(cwd)]
        if self.cfg.model:
            argv += ["-m", self.cfg.model]
        if self.cfg.effort:
            argv += ["-c", f"model_reasoning_effort={self.cfg.effort}"]
        argv += [*self.cfg.args, "-"]
        return Invocation(argv, prompt, self.base_env())

    def open_command(self, session_id: str, cwd: Path) -> list[str] | None:
        return [*self.cfg.command, "resume", session_id]


class OpenCodeAdapter(Adapter):
    parser_class = OpenCodeParser

    def invocation(self, prompt: str, prompt_file: Path, cwd: Path, *, read_only: bool = False,
                   resume_id: str | None = None) -> Invocation:
        argv = [*self.cfg.command, "run", *([] if read_only else ["--auto"]), "--format", "json"]
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

    def open_command(self, session_id: str, cwd: Path) -> list[str] | None:
        return [*self.cfg.command, str(cwd), "--session", session_id]


class CustomAdapter(Adapter):
    def invocation(self, prompt: str, prompt_file: Path, cwd: Path, *, read_only: bool = False,
                   resume_id: str | None = None) -> Invocation:
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
