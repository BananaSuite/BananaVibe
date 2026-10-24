"""Live view of a run: the supervisor's own log plus the full transcript of the agent session in progress.

Session logs hold the agents' raw event streams (one JSON object per line). The renderers below turn them
into a readable transcript: the agent's messages and thinking, every tool call with its input, and the
tool output, which is what you would see in the agent's own terminal UI.

Everything shown is sanitized first: tool output often contains colour codes, progress bars, cursor
movement or even "clear screen" sequences, which would otherwise garble the terminal.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from bananavibe.config import Config
from bananavibe.state import Paths
from bananavibe.util import ascii_only, clean, existing_log, open_log, read_json, read_tail, symbol, use_color

AGENT_LINE = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d   \[")


class Style:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color and code else text


def _clip(text: str, max_lines: int, width: int = 400) -> list[str]:
    lines = [ln if len(ln) <= width else ln[:width] + "…" for ln in clean(str(text)).rstrip().splitlines()]
    if max_lines and len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [f"… {hidden} more line(s) (use --full to see everything)"]
    return lines


def _block_text(content) -> str:
    """Tool results come as a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get("text") or (f"[{b.get('type')}]" if b.get("type") else ""))
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    return json.dumps(content) if content is not None else ""


def _tool_input(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp)
    if set(inp) <= {"command", "description", "timeout"} and inp.get("command"):
        return "$ " + str(inp["command"])
    return json.dumps(inp, ensure_ascii=False, indent=1)


Part = tuple[str, str]  # (style code, plain text)


class Renderer:
    """Renders one session's raw log lines for a given agent type."""

    def __init__(self, agent_type: str, style: Style, max_lines: int = 25, ascii_mode: bool = False):
        self.type = agent_type
        self.s = style
        self.max = max_lines
        self.ascii = ascii_mode

    def sym(self, name: str) -> str:
        return symbol(name, self.ascii)

    def _out(self, prefix: str, code: str, text: str, max_lines: int | None = None) -> list[Part]:
        lines = _clip(text, self.max if max_lines is None else max_lines)
        if not lines:
            return []
        pad = " " * len(prefix)
        return [(code, (prefix if i == 0 else pad) + ln) for i, ln in enumerate(lines)]

    def render(self, line: str) -> list[str]:
        return [self.s(code, text) for code, text in self.render_parts(line)]

    def render_parts(self, line: str) -> list[Part]:
        line = line.rstrip("\n")
        if line.startswith("# argv:") or line.startswith("# exit code") or line.startswith("# failed"):
            return [("2", clean(line))]
        try:
            ev = json.loads(line)
        except ValueError:
            text = clean(line)
            return [("", text)] if text.strip() else []
        if not isinstance(ev, dict):
            return []
        fn = getattr(self, f"_{self.type}", None)
        try:
            return fn(ev) if fn else [("", clean(line))]
        except (AttributeError, TypeError, ValueError, KeyError):
            return [("2", clean(line)[:400])]  # an event shape we don't know: show it raw rather than fail

    def _claude(self, ev: dict) -> list[Part]:
        t = ev.get("type")
        out: list[Part] = []
        if t == "system" and ev.get("subtype") == "init":
            return [("2", f"{self.sym('dot')} session {ev.get('session_id', '?')} {self.sym('dot')} model "
                          f"{ev.get('model', '?')} {self.sym('dot')} cwd {ev.get('cwd', '?')}")]
        if t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                kind = b.get("type")
                if kind == "text" and b.get("text", "").strip():
                    out += self._out(self.sym("say") + " ", "1", b["text"], 0)
                elif kind == "thinking" and b.get("thinking", "").strip():
                    out += self._out(self.sym("think") + " ", "2;3", b["thinking"])
                elif kind == "redacted_thinking":
                    out.append(("2;3", self.sym("think") + " (thinking)"))
                elif kind == "tool_use":
                    out += self._out(f"{self.sym('tool')} {b.get('name')} ", "36", _tool_input(b.get("input")))
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    text = _block_text(b.get("content")) or "(no output)"
                    out += self._out(f"   {self.sym('out')} ", "31" if b.get("is_error") else "2", text)
        elif t == "result":
            cost = ev.get("total_cost_usd")
            out.append(("32" if not ev.get("is_error") else "31",
                        f"{self.sym('result')} result: {ev.get('subtype', '')} {self.sym('dot')} "
                        f"{ev.get('num_turns', '?')} turns"
                        + (f" {self.sym('dot')} ${cost:.2f}" if isinstance(cost, (int, float)) else "")))
        elif t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed"):
                out.append(("33", f"{self.sym('wait')} rate limit {info.get('status')} "
                                  f"({info.get('rateLimitType', '?')})"))
        elif t == "system" and ev.get("subtype"):
            out.append(("2", f"{self.sym('dot')} {ev.get('subtype')}"))
        return out

    def _codex(self, ev: dict) -> list[Part]:
        t = ev.get("type")
        if t == "thread.started":
            return [("2", f"{self.sym('dot')} thread {ev.get('thread_id', '?')}")]
        if t == "turn.completed":
            u = ev.get("usage") or {}
            return [("32", f"{self.sym('result')} turn completed {self.sym('dot')} {u.get('input_tokens', '?')} in / "
                           f"{u.get('output_tokens', '?')} out tokens")]
        if t == "turn.failed":
            return self._out(self.sym("fail") + " ", "31", str((ev.get("error") or {}).get("message", ev)))
        if t == "error":
            return self._out(self.sym("warn") + " ", "33", str(ev.get("message", "")))
        if t not in ("item.started", "item.completed", "item.updated"):
            return []
        item = ev.get("item") or {}
        kind = item.get("type") or item.get("item_type")
        done = t == "item.completed"
        if kind == "agent_message" and done:
            return self._out(self.sym("say") + " ", "1", item.get("text", ""), 0)
        if kind == "reasoning" and done:
            return self._out(self.sym("think") + " ", "2;3", item.get("text", ""))
        if kind == "command_execution":
            if t == "item.started":
                return self._out(f"{self.sym('tool')} $ ", "36", item.get("command", ""))
            if done:
                code = item.get("exit_code")
                text = item.get("aggregated_output") or "(no output)"
                return self._out(f"   {self.sym('out')} ", "31" if code not in (0, None) else "2", text) + (
                    [("2", f"     exit {code}")] if code not in (0, None) else [])
        if kind == "file_change" and done:
            return [("36", clean(f"{self.sym('edit')}  {c.get('kind', 'edit')} {c.get('path', '')}"))
                    for c in item.get("changes") or [] if isinstance(c, dict)]
        if kind == "mcp_tool_call" and t == "item.started":
            return self._out(f"{self.sym('tool')} {item.get('server', '')}.{item.get('tool', '')} ", "36",
                             _tool_input(item.get("arguments")))
        if kind == "web_search" and t == "item.started":
            return [("36", clean(f"{self.sym('search')} {item.get('query', '')}"))]
        if kind == "todo_list":
            return [("35", clean((self.sym("todo_done") if i.get("completed") else self.sym("todo")) + " "
                                 + str(i.get("text", ""))))
                    for i in item.get("items") or [] if isinstance(i, dict)] if done or t == "item.updated" else []
        if kind == "error" and done:
            return self._out(self.sym("warn") + " ", "33", item.get("message", ""))
        return []

    def _opencode(self, ev: dict) -> list[Part]:
        t = ev.get("type")
        part = ev.get("part") or {}
        if t == "text" and part.get("text", "").strip():
            return self._out(self.sym("say") + " ", "1", part["text"], 0)
        if t == "reasoning" and part.get("text", "").strip():
            return self._out(self.sym("think") + " ", "2;3", part["text"])
        if t in ("tool_use", "tool"):
            state = part.get("state") or {}
            out = self._out(f"{self.sym('tool')} {part.get('tool', '?')} ", "36", _tool_input(state.get("input") or {}))
            if state.get("output"):
                out += self._out(f"   {self.sym('out')} ", "31" if state.get("status") == "error" else "2",
                                 str(state["output"]))
            return out
        if t == "step_finish":
            tok = part.get("tokens") or {}
            return [("2", f"{self.sym('dot')} step finished {self.sym('dot')} {tok.get('input', '?')} in / "
                          f"{tok.get('output', '?')} out")]
        if t == "error" or ev.get("error"):
            return self._out(self.sym("fail") + " ", "31", json.dumps(ev.get("error") or part.get("error") or ev))
        return []

    def _custom(self, ev: dict) -> list[Part]:
        return [("", clean(json.dumps(ev)))]


def renderer_for(cfg: Config, agent: str, style: Style, max_lines: int, ascii_mode: bool = False) -> Renderer:
    a = cfg.agents.get(agent)
    return Renderer(a.type if a else "custom", style, max_lines, ascii_mode)


def agent_from_log(name: str) -> str:
    """'0003-work-claude-20261008-101500.log' -> 'claude' (agent names may contain dashes)."""
    m = re.match(r"^\d{4}-(?:work|review-final|review-checkpoint)-(.+)-\d{8}-\d{6}\.log(?:\.gz)?$", name)
    return m.group(1) if m else ""


def render_file(path: Path, renderer: Renderer, raw: bool = False) -> list[str]:
    out: list[str] = []
    for line in iter_file(path, renderer, raw):
        out.append(line)
    return out


def iter_file(path: Path, renderer: Renderer, raw: bool = False):
    """Rendered lines of a session log, streamed (logs can be large), compressed or not."""
    with open_log(path) as f:
        for line in f:
            if raw:
                yield clean(line.rstrip("\n"))
            else:
                yield from renderer.render(line)


def latest_session_log(paths: Paths) -> Path | None:
    if not paths.logs.exists():
        return None
    logs = [p for p in paths.logs.iterdir() if agent_from_log(p.name)]
    if not logs:
        return None
    return max(logs, key=lambda p: (p.stat().st_mtime if p.exists() else 0, p.name))


class _Tail:
    """Reads complete lines appended to a file since the last call. Survives truncation and rotation."""

    def __init__(self, path: Path, from_end: bool):
        self.path = path
        self.ino = self._ino()
        self.pos = path.stat().st_size if from_end and path.exists() else 0
        self.buf = ""

    def _ino(self) -> int | None:
        try:
            return self.path.stat().st_ino
        except OSError:
            return None

    def read(self, max_bytes: int = 4_000_000) -> list[str]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if st.st_ino != self.ino:  # replaced (rotated): start the new file from the beginning
            self.ino, self.pos, self.buf = st.st_ino, 0, ""
        if st.st_size < self.pos:  # truncated
            self.pos = 0
        if st.st_size == self.pos:
            return []
        with self.path.open("rb") as f:
            if st.st_size - self.pos > max_bytes:  # far behind: skip to the recent part
                self.pos = st.st_size - max_bytes
                self.buf = ""
            f.seek(self.pos)
            data = f.read(max_bytes)
            self.pos = f.tell()
        text = self.buf + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self.buf = lines.pop()
        return lines


def follow(paths: Paths, cfg: Config, raw: bool = False, max_lines: int = 25, history: int = 40,
           once: bool = False, out=None) -> int:
    """Print the run's live activity until interrupted (Ctrl-C). Returns an exit code."""
    out = out or sys.stdout
    style = Style(use_color(out))
    asc = ascii_only(out)

    def emit(lines: list[str]) -> None:
        for ln in lines:
            print(ln, file=out, flush=True)

    sup_lines = [ln for ln in read_tail(paths.supervisor_log, 200_000).splitlines() if not AGENT_LINE.match(ln)]
    emit([style("33", clean(ln)) for ln in sup_lines[-12:]])
    sup = _Tail(paths.supervisor_log, from_end=True)
    session: _Tail | None = None
    session_path = ""
    renderer: Renderer | None = None
    first = True
    try:
        while True:
            emit([style("33", clean(ln)) for ln in sup.read() if ln and not AGENT_LINE.match(ln)])
            state = read_json(paths.state, {}) or {}
            current = state.get("current") or {}
            log = current.get("log") or ""
            if not log and first:
                # Nothing running: show the end of the latest session instead.
                latest = latest_session_log(paths)
                if latest:
                    log = paths.rel(latest)
            if log and log != session_path:
                session_path = log
                path = existing_log(paths.workspace / log)
                agent = current.get("agent") if current.get("log") == log else agent_from_log(path.name)
                renderer = renderer_for(cfg, agent or "", style, max_lines, asc)
                emit([style("1;35", f"──── {log} ({agent or '?'}"
                                    + (f", session {current['session_id']}" if current.get("session_id") else "")
                                    + ") ────")])
                lines = []
                if path.name.endswith(".gz"):
                    session = None
                    lines = list(iter_file(path, renderer, raw))
                else:
                    session = _Tail(path, from_end=False)
                    for ln in session.read():
                        lines += [clean(ln)] if raw else renderer.render(ln)
                if first and history and len(lines) > history:
                    emit([style("2", f"… {len(lines) - history} earlier line(s); `bananavibe show` has them")])
                    lines = lines[-history:]
                emit(lines)
            elif session and renderer:
                for ln in session.read():
                    emit([clean(ln)] if raw else renderer.render(ln))
            if first and not state.get("pid"):
                emit([style("2", f"(no supervisor running: status {state.get('status', 'new')}. "
                                 "Waiting for activity; Ctrl-C to quit)")])
            first = False
            if once:
                return 0
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def is_tty(stream=None) -> bool:
    try:
        return (stream or sys.stdout).isatty()
    except (AttributeError, ValueError):
        return False


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0")
