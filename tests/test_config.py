import tomllib
from pathlib import Path

import pytest

from bananavibe import templates
from bananavibe.config import ConfigError, parse
from bananavibe.state import plan_stats


def test_template_parses(tmp_path: Path):
    text = templates.CONFIG.format(name="x", workers='["claude", "codex"]', reviewer="codex",
                                   checks=templates.CHECK.format(name='"tests"', run='"pytest -q \\"a b\\""'),
                                   claude_model="", claude_effort="max", codex_model="gpt-5.6-terra",
                                   codex_effort="ultra", opencode_model="")
    cfg = parse(tomllib.loads(text), tmp_path)
    assert cfg.workers == ["claude", "codex"] and cfg.reviewer_name == "codex"
    assert cfg.checks[0].run == 'pytest -q "a b"'
    assert cfg.agents["codex"].effort == "ultra"
    assert cfg.agents["opencode"].type == "opencode"


def test_defaults(tmp_path: Path):
    cfg = parse({}, tmp_path)
    assert cfg.workers == ["claude"] and cfg.reviewer_name == "claude" and "claude" in cfg.agents
    # Long-run safety defaults: never exit after done, restart at boot, guard the machine's resources.
    assert cfg.after_done == "wait" and cfg.autostart and cfg.watchdog_minutes > 0
    assert cfg.agent_oom_score_adj > 0 and cfg.min_free_disk_mb > 0 and cfg.logs_max_mb > 0


@pytest.mark.parametrize("data,message", [
    ({"run": {"workers": ["nope"]}}, "not defined"),
    ({"run": {"strategy": "random"}}, "strategy"),
    ({"run": {"typo": 1}}, "unknown key"),
    ({"checks": [{"name": "x"}]}, "needs a 'run'"),
    ({"agents": {"mine": {"type": "custom", "command": ["tool"]}}}, "{prompt}"),
    ({"run": {"max_iterations": "5"}}, "must be of type"),
    ({"run": {"after_done": "sleep"}}, "after_done"),
    ({"run": 5}, "must be a table"),
    ({"notify": {"url": "ftp://x"}}, "http"),
    ({"notify": {"format": "xml"}}, "format"),
    ({"notify": {"events": ["done", "party"]}}, "unknown event"),
    ({"notify": {"headers": {"A": 1}}}, "table of strings"),
    ({"agents": {"../evil": {"type": "custom", "command": ["x", "{prompt}"]}}}, "may only contain"),
    ({"logs": {"max_mb": -1}}, "negative"),
    ({"retry": {"initial_seconds": 0}}, "positive"),
    ({"checks": [{"run": "x", "timeout_minutes": 0}]}, "timeout_minutes"),
])
def test_errors(tmp_path: Path, data, message):
    with pytest.raises(ConfigError, match=message.replace("{", r"\{").replace("}", r"\}")):
        parse(data, tmp_path)


def test_plan_stats():
    s = plan_stats("# Plan\n- [x] a\n- [ ] b\n  * [!] c (blocked)\n- [~] d\n- [X] e\nnot an item\n")
    assert (s.total, s.done, s.open, s.blocked, s.dropped) == (5, 2, 1, 1, 1)
    assert s.open_items == ["b", "c (blocked)"]
