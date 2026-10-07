import datetime as dt
import json
from pathlib import Path

import pytest

from bananavibe.adapters import (
    AUTH,
    FATAL,
    LIMIT,
    OK,
    TRANSIENT,
    ClaudeAdapter,
    CodexAdapter,
    CodexParser,
    OpenCodeAdapter,
    classify_error,
    make_adapter,
    parse_retry_at,
)
from bananavibe.config import AgentConfig

REF = dt.datetime(2026, 10, 7, 13, 0, tzinfo=dt.UTC)


@pytest.mark.parametrize("text,kind", [
    ("Claude AI usage limit reached|1791400000", LIMIT),
    ("You've hit your limit · resets 3pm (Europe/Rome)", LIMIT),
    ("You've hit your usage limit. Upgrade to Pro or try again in 2 days 3 hours 5 minutes.", LIMIT),
    ('{"type":"error","status":429,"error":{"type":"rate_limit_error"}}', LIMIT),
    ("API Error: 529 {\"type\":\"overloaded_error\",\"message\":\"Overloaded\"}", TRANSIENT),
    ("stream disconnected before completion: Connection reset by peer", TRANSIENT),
    ("Invalid API key · Please run /login", AUTH),
    ("OAuth token has expired. Please obtain a new token.", AUTH),
    ("The 'gpt-x' model is not supported when using Codex with a ChatGPT account.", FATAL),
    ('{"type":"provider.no-route","message":"Model unavailable: opencode/nope"}', FATAL),
    ("", TRANSIENT),
])
def test_classify(text, kind):
    assert classify_error(text)[0] == kind


def test_retry_at_epoch():
    t = parse_retry_at("Claude AI usage limit reached|1791400000", REF)
    assert t == dt.datetime.fromtimestamp(1791400000).astimezone()


def test_retry_at_relative():
    assert parse_retry_at("try again in 2 days 3 hours 5 minutes", REF) == REF + dt.timedelta(days=2, hours=3, minutes=5)
    assert parse_retry_at("Please try again in 45s.", REF) == REF + dt.timedelta(seconds=45)
    assert parse_retry_at("retry-after: 120", REF) == REF + dt.timedelta(seconds=120)


def test_retry_at_clock_time_with_zone():
    t = parse_retry_at("5-hour limit reached ∙ resets 3pm (Europe/Rome)", REF)
    # 13:00 UTC is 15:00 in Rome, so 3pm today has just passed: the next 3pm Rome is tomorrow.
    assert t == dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.UTC)
    t = parse_retry_at("Try again at 4:30 PM", REF.astimezone())
    assert t.hour == 16 and t.minute == 30


def test_retry_at_month_day():
    t = parse_retry_at("Weekly limit reached · resets Oct 9, 10am (UTC)", REF)
    assert t == dt.datetime(2026, 10, 9, 10, 0, tzinfo=dt.UTC)


def test_retry_at_codex_style_date():
    t = parse_retry_at("You've hit your usage limit. Upgrade to Pro or try again at Oct 9th, 2026 3:05 PM.",
                       REF.astimezone())
    assert (t.year, t.month, t.day, t.hour, t.minute) == (2026, 10, 9, 15, 5)


def test_real_codex_free_plan_limit_message():
    msg = ("You’ve hit your usage limit. To continue using Codex and get access to GPT-5.3-Codex, start a free trial "
           "of Plus today (https://chatgpt.com/explore/plus), or try again at Nov 4th, 2026 8:39 PM.")
    kind, t = classify_error(msg)
    assert kind == LIMIT and (t.month, t.day, t.hour, t.minute) == (11, 4, 20, 39)


def test_retry_at_unknown():
    assert parse_retry_at("usage limit reached", REF) is None


CLAUDE_OK = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "PONG"}]}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1791408600}},
    {"type": "result", "subtype": "success", "is_error": False, "result": "PONG", "num_turns": 1,
     "total_cost_usd": 0.07},
]


def feed(parser, events):
    shown = []
    for e in events:
        shown += parser.feed(json.dumps(e) if isinstance(e, dict) else e)
    return shown


def test_claude_parser_success():
    p = make_adapter(AgentConfig("claude", "claude", ["claude"])).parser()
    shown = feed(p, CLAUDE_OK)
    assert any("Bash ls" in s for s in shown)
    out = p.finish(0)
    assert out.kind == OK and out.final_text == "PONG"
    assert out.usage["cost_usd"] == 0.07


def test_claude_parser_rejected_limit_uses_reset_time():
    p = make_adapter(AgentConfig("claude", "claude", ["claude"])).parser()
    feed(p, [
        {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1791408600,
                                                         "rateLimitType": "five_hour"}},
        {"type": "result", "subtype": "success", "is_error": True, "result": "You've hit your limit · resets 3pm"},
    ])
    out = p.outcome(1)
    assert out.kind == LIMIT
    assert out.retry_at == dt.datetime.fromtimestamp(1791408600).astimezone()


def test_codex_parser_failure_and_success():
    p = CodexParser()
    feed(p, [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "error", "message": "Model metadata not found"}},
        {"type": "turn.started"},
        {"type": "error", "message": '{"status":400,"error":{"type":"invalid_request_error","message":"The '
                                     "'gpt-x' model is not supported when using Codex with a ChatGPT account.\"}}"},
        {"type": "turn.failed", "error": {"message": "model is not supported"}},
    ])
    assert p.outcome(1).kind == FATAL

    p = CodexParser()
    feed(p, [
        {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected)"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "pytest"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "All done"}},
        {"type": "turn.completed", "usage": {}},
    ])
    out = p.outcome(0)
    assert out.kind == OK and out.final_text == "All done"


def test_opencode_parser():
    a = make_adapter(AgentConfig("opencode", "opencode", ["opencode"]))
    p = a.parser()
    feed(p, [{"type": "step_start", "part": {}},
             {"type": "tool_use", "part": {"tool": "shell", "state": {"input": {"command": "ls -la"}}}},
             {"type": "text", "part": {"text": "DONE"}}])
    assert p.outcome(0).kind == OK
    p = a.parser()
    feed(p, [{"type": "error", "error": {"type": "provider.no-route", "message": "Model unavailable: x/y"}}])
    assert p.outcome(1).kind == FATAL
    p = a.parser()
    assert p.outcome(0).kind == TRANSIENT  # exited cleanly but said nothing at all


def test_invocations(tmp_path: Path):
    pf = tmp_path / "p.md"
    inv = ClaudeAdapter(AgentConfig("claude", "claude", ["claude"], model="opus", effort="max")).invocation(
        "hello", pf, tmp_path)
    assert inv.stdin == "hello"
    assert "--dangerously-skip-permissions" in inv.argv and ["--model", "opus"] == inv.argv[6:8]
    inv = CodexAdapter(AgentConfig("codex", "codex", ["codex"], model="m", effort="ultra")).invocation(
        "hello", pf, tmp_path)
    assert inv.argv[-1] == "-" and "model_reasoning_effort=ultra" in inv.argv and inv.stdin == "hello"
    inv = OpenCodeAdapter(AgentConfig("opencode", "opencode", ["opencode"], model="a/b")).invocation(
        "hello", pf, tmp_path)
    assert inv.stdin is None and str(pf) in inv.argv[-1] and "--auto" in inv.argv
    inv = make_adapter(AgentConfig("x", "custom", ["tool", "--file", "{prompt_file}"])).invocation("hi", pf, tmp_path)
    assert inv.argv == ["tool", "--file", str(pf)]
