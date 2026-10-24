"""The terminal UI: dashboard, chat, status, transcripts. Output must fit the terminal and never carry
escape sequences from agent output."""

import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from conftest import configure, run_supervisor

from bananavibe import chat, handover, tui
from bananavibe.cli import main, status_lines
from bananavibe.config import load
from bananavibe.state import Paths, State, pending_messages
from bananavibe.util import strip_styles, text_width

ROOT = os.path.dirname(os.path.dirname(__file__))
ESC = re.compile(r"\x1b(?!\[[0-9;]*m)")  # any escape sequence other than our own colours


def test_dashboard_frame_fits_the_terminal(workspace, monkeypatch):
    paths = configure(workspace, ["ansi", "finish", "approve"])
    run_supervisor(paths)
    cfg = load(paths.config, paths.workspace)
    for cols, rows in ((60, 18), (120, 40), (30, 10)):
        monkeypatch.setattr(shutil, "get_terminal_size", lambda fallback=None, c=cols, r=rows: os.terminal_size((c, r)))
        frame = tui.Dashboard(paths, cfg).frame()
        assert len(frame) == rows
        for line in frame:
            assert text_width(strip_styles(line)) <= cols, line
            assert not ESC.search(line), repr(line)
        if cols >= 100:
            assert "q quit" in strip_styles(frame[-1]) and "Plan" in "\n".join(frame)


def test_transcripts_are_sanitized(workspace, capsys):
    paths = configure(workspace, ["ansi", "finish", "approve"])
    run_supervisor(paths)
    capsys.readouterr()
    assert main(["-w", str(workspace), "show", "1"]) == 0
    out = capsys.readouterr().out
    assert "progress 100%" in out and "red" in out
    assert "\x1b" not in out and "\x07" not in out
    assert "\x1b" not in paths.supervisor_log.read_text().replace("\x1b[", "")  # agent lines are cleaned


def test_compressed_session_logs_can_still_be_shown(workspace, capsys):
    paths = configure(workspace, ["plan", "finish", "approve"])
    run_supervisor(paths)
    log = paths.workspace / State(paths)["history"][0]["log"]
    with log.open("rb") as src, gzip.open(log.with_name(log.name + ".gz"), "wb") as dst:
        dst.write(src.read())
    log.unlink()
    capsys.readouterr()
    assert main(["-w", str(workspace), "show", "1"]) == 0
    assert "# argv:" in capsys.readouterr().out


def test_chat_answers_questions_and_takes_instructions(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    run_supervisor(paths)
    cfg = load(paths.config, paths.workspace)
    out = io.StringIO()
    stdin = io.StringIO("/say Please add a changelog.\nis it done?\n/status\n/history\n/bogus\n/quit\n")
    assert chat.run(paths, cfg, stdin=stdin, out=out) == 0
    text = out.getvalue()
    assert "the run status is done" in text
    assert "Status" in text and "Plan" in text
    assert "unknown command /bogus" in text
    assert [m.text for m in pending_messages(paths)] == ["Please add a changelog."]


def test_status_screen(workspace):
    paths = configure(workspace, ["plan", "work:a.txt"], extra="max_iterations = 2")
    run_supervisor(paths)
    lines = status_lines(paths, events=3)
    text = "\n".join(lines)
    assert "Plan" in text and "1/2 done" in text and "Autostart" in text and "stopped" in text
    assert all("\x1b" not in ln for ln in lines)


def test_cli_survives_a_closed_pipe(workspace):
    paths = configure(workspace, ["plan", "finish", "approve"])
    run_supervisor(paths)
    p = subprocess.Popen(f"{sys.executable} -m bananavibe -w {workspace} show 1 --full | head -1", shell=True,
                         cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=60)
    assert "Traceback" not in err and "BrokenPipe" not in err


def test_ask_cli_without_init_is_a_clear_error(tmp_path):
    r = subprocess.run([sys.executable, "-m", "bananavibe", "-w", str(tmp_path), "pause"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "init" in r.stderr and not (tmp_path / ".bananavibe").exists()


def _tamper(archive: Path, out: Path) -> None:
    with tarfile.open(archive) as src, tarfile.open(out, "w:gz") as dst:
        for m in src.getmembers():
            data = src.extractfile(m).read() if m.isfile() else None
            if m.name.endswith(".bundle"):
                data = data[:-40] + b"0" * 40
                m.size = len(data)
            dst.addfile(m, io.BytesIO(data) if data is not None else None)


def test_import_detects_a_corrupted_archive(workspace, tmp_path):
    paths = configure(workspace, ["plan"], extra="max_iterations = 1")
    run_supervisor(paths)
    archive = tmp_path / "run.tar.gz"
    assert main(["-w", str(workspace), "export", "-o", str(archive)]) == 0
    assert State(paths)["operator_stop"] is True  # the old machine won't restart it by itself
    bad = tmp_path / "bad.tar.gz"
    _tamper(archive, bad)
    other = tmp_path / "other"
    try:
        handover.import_run(bad, other)
        raise AssertionError("a damaged archive was accepted")
    except handover.HandoverError as e:
        assert "corrupted" in str(e)
    assert not (other / ".bananavibe").exists()


def test_interrupted_claude_conversation_travels_with_the_run(workspace, tmp_path, monkeypatch):
    paths = configure(workspace, ["plan"], extra="max_iterations = 1")
    run_supervisor(paths)
    paths.config.write_text(paths.config.read_text() + '\n[agents.claude]\nmodel = ""\n')
    st = State(paths)
    st["interrupted"] = {"label": "work", "agent": "claude", "host": "old-host", "session_id": "abc-123456",
                         "activity": [], "reason": "stop now", "started": "x"}
    st.save()
    old_home = tmp_path / "claude-old"
    proj = old_home / "projects" / handover.claude_project_name(workspace)
    proj.mkdir(parents=True)
    (proj / "abc-123456.jsonl").write_text(json.dumps({"type": "user", "cwd": str(workspace)}) + "\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(old_home))
    archive = tmp_path / "run.tar.gz"
    manifest = handover.export(paths, load(paths.config, paths.workspace), archive)
    assert manifest["agent_sessions"][0]["session_id"] == "abc-123456"

    new_home = tmp_path / "claude-new"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(new_home))
    other = tmp_path / "elsewhere" / "ws2"
    done = handover.import_run(archive, other)
    assert any("Claude Code conversation" in d for d in done)
    moved = new_home / "projects" / handover.claude_project_name(other) / "abc-123456.jsonl"
    assert json.loads(moved.read_text())["cwd"] == str(other)
    import socket
    assert State(Paths(other))["interrupted"]["host"] == socket.gethostname()
