"""Tests for `omnilab doctor` and the root `--json` flag."""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from typer.testing import CliRunner

from omnilab.cli import app


def _run(args: list[str]) -> tuple[int, str]:
    """Helper: invoke CLI and return (exit_code, output)."""
    runner = CliRunner()
    result = runner.invoke(app, args)
    return result.exit_code, result.output


def test_version_human():
    code, out = _run(["version"])
    assert code == 0
    assert "omnilab" in out


def test_version_json():
    code, out = _run(["--json", "version"])
    assert code == 0
    parsed = json.loads(out)
    assert "version" in parsed
    assert isinstance(parsed["version"], str)


def test_doctor_json_returns_valid_schema(tmp_path: Path):
    # No omnilab.yaml in tmp_path, so manifest check fails — but the
    # command should still emit a structured doctor report.
    with patch("omnilab.cli.has_podman", return_value=False), \
         patch("omnilab.cli.detect_gpu", return_value="none"):
        code, out = _run(["--json", "doctor", "--directory", str(tmp_path)])

    # Doctor exits with the count of failed checks. With no podman, no
    # GPU, and no manifest in tmp_path, we expect 3 failures.
    assert code >= 1
    parsed = json.loads(out)
    assert "passed" in parsed
    assert "failed" in parsed
    assert "checks" in parsed
    assert isinstance(parsed["checks"], list)
    for check in parsed["checks"]:
        assert "name" in check
        assert "ok" in check
        assert isinstance(check["ok"], bool)


def test_doctor_human_prints_sections(tmp_path: Path):
    with patch("omnilab.cli.has_podman", return_value=False), \
         patch("omnilab.cli.detect_gpu", return_value="none"):
        code, out = _run(["doctor", "--directory", str(tmp_path)])
    assert code >= 1
    assert "=== environment ===" in out
    assert "=== manifest ===" in out
    assert "Result:" in out


def test_new_json_emits_project_payload(tmp_path: Path):
    code, out = _run(
        ["--json", "new", "test-foo", "--directory", str(tmp_path / "test-foo")]
    )
    assert code == 0
    parsed = json.loads(out)
    assert parsed["project"] == "test-foo"
    assert parsed["template"] == "ros-jazzy-gz-harmonic"
    assert parsed["path"].endswith("test-foo")
    assert "manifest_path" in parsed
    # And the file actually exists.
    assert Path(parsed["manifest_path"]).exists()


def test_new_json_invalid_template_uses_exit_code_2(tmp_path: Path):
    code, out = _run(
        [
            "--json",
            "new",
            "test-bar",
            "--template",
            "no-such",
            "--directory",
            str(tmp_path / "test-bar"),
        ]
    )
    # Exit 2 = invalid args per spec.
    assert code == 2
    err = json.loads(out)
    assert err["code"] == 2
    assert "unknown template" in err["error"]


def test_root_help_lists_json_flag():
    code, out = _run(["--help"])
    assert code == 0
    # Rich-rendered help can wrap "--json" in ANSI codes that split the
    # literal substring across colour escape sequences. Check the help
    # text instead, which we control verbatim and Rich won't fragment.
    assert "machine-readable JSON" in out


def test_down_dry_run_does_not_call_podman(tmp_path: Path):
    """`down --dry-run` must not invoke podman stop."""
    # Create a minimal project so manifest loads.
    project = tmp_path / "drytest"
    _run(["new", "drytest", "--directory", str(project)])

    stop_calls = []

    def fake_stop(name):
        stop_calls.append(name)
        return CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("omnilab.cli.has_podman", return_value=True), \
         patch("omnilab.cli.container_running", return_value=True), \
         patch("omnilab.cli.stop_container", side_effect=fake_stop):
        code, _ = _run(["down", "--dry-run", "--directory", str(project)])

    assert code == 0
    assert stop_calls == [], "podman stop must not be called in dry-run"


def test_down_json_requires_yes(tmp_path: Path):
    """JSON mode + destructive command without --yes → exit 2."""
    project = tmp_path / "yestest"
    _run(["new", "yestest", "--directory", str(project)])
    with patch("omnilab.cli.has_podman", return_value=True), \
         patch("omnilab.cli.container_running", return_value=True):
        code, out = _run(["--json", "down", "--directory", str(project)])
    assert code == 2
    parsed = json.loads(out)
    assert "error" in parsed
