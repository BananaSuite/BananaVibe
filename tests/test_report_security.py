"""Task reports must not read or unlink files outside the coding workspace."""

import json
import os

import pytest

from bananavibe.workspace import clear_task_report, read_task_json


def test_reports_cannot_follow_a_linked_parent_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    controller = tmp_path / "controller"
    controller.mkdir()
    secret = controller / "result.json"
    secret.write_text(json.dumps({"status": "blocked", "question": "private controller data"}))
    (project / ".bananavibe-task").symlink_to(controller, target_is_directory=True)

    assert read_task_json(project, "result.json", 32000) is None
    with pytest.raises(OSError):
        clear_task_report(project)
    assert secret.exists()


def test_report_read_rejects_links_fifos_and_large_files(tmp_path):
    reports = tmp_path / ".bananavibe-task"
    reports.mkdir()
    report = reports / "result.json"
    outside = tmp_path / "private.json"
    outside.write_text('{"status":"complete"}')
    report.symlink_to(outside)
    assert read_task_json(tmp_path, "result.json", 100) is None
    clear_task_report(tmp_path)
    assert outside.exists()

    os.mkfifo(report)
    assert read_task_json(tmp_path, "result.json", 100) is None
    clear_task_report(tmp_path)
    report.write_text(json.dumps({"body": "x" * 101}))
    assert read_task_json(tmp_path, "result.json", 100) is None


def test_regular_report_is_read_and_removed(tmp_path):
    reports = tmp_path / ".bananavibe-task"
    reports.mkdir()
    report = reports / "result.json"
    report.write_text('{"status":"complete"}')
    assert read_task_json(tmp_path, "result.json", 32000) == {"status": "complete"}
    clear_task_report(tmp_path)
    assert not report.exists()
    clear_task_report(tmp_path)
