from pathlib import Path

from bananavibe import __version__
from bananavibe.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_check_config_new_and_legacy_forms(capsys):
    assert main(["check-config", "--config", str(ROOT / "examples/bananawiki.toml")]) == 0
    assert "Valid github configuration" in capsys.readouterr().out
    assert main(["--config", str(ROOT / "examples/bananachat.toml"), "--check-config"]) == 0


def test_errors_are_reported_without_a_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    broken = tmp_path / "broken.toml"
    broken.write_text("forge = 'gitlab'\n")
    assert main(["check-config", "--config", str(broken)]) == 1
    assert "forge must be" in capsys.readouterr().err


def test_run_without_an_event_explains_itself(capsys, monkeypatch):
    for name in ("GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME", "FORGEJO_EVENT_PATH", "FORGEJO_EVENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main(["--config", str(ROOT / "examples/bananawiki.toml")]) == 1
    assert "::error::" in capsys.readouterr().out


def test_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
