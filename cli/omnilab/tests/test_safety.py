"""Tests for omnilab._safety — confirm_or_exit destructive flow."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import pytest
import typer

from omnilab import _output, _safety


@pytest.fixture(autouse=True)
def reset_mode():
    _output.set_json_mode(False)
    yield
    _output.set_json_mode(False)


def test_dry_run_human_exits_clean(capsys):
    with pytest.raises(typer.Exit) as e:
        _safety.confirm_or_exit(
            summary="Stop container 'foo'?",
            items=["podman stop foo"],
            dry_run=True,
        )
    assert e.value.exit_code == 0
    out = capsys.readouterr().out
    assert "Stop container 'foo'?" in out
    assert "podman stop foo" in out
    assert "Dry-run" in out


def test_yes_human_skips_prompt():
    # Should return without raising, without prompting.
    _safety.confirm_or_exit(
        summary="x", items=["y"], yes=True, dry_run=False
    )


def test_default_human_aborts_on_no(capsys):
    with patch("typer.confirm", return_value=False), pytest.raises(typer.Exit) as e:
        _safety.confirm_or_exit(summary="x", items=["y"])
    assert e.value.exit_code == 0
    assert "Aborted" in capsys.readouterr().out


def test_default_human_proceeds_on_yes():
    with patch("typer.confirm", return_value=True):
        _safety.confirm_or_exit(summary="x", items=["y"])


def test_json_dry_run_emits_payload():
    _output.set_json_mode(True)
    fake = StringIO()
    with patch("sys.stdout", fake), pytest.raises(typer.Exit) as e:
        _safety.confirm_or_exit(
            summary="Stop foo",
            items=["podman stop foo"],
            dry_run=True,
            json_payload={"container": "foo"},
        )
    assert e.value.exit_code == 0
    parsed = json.loads(fake.getvalue())
    assert parsed["container"] == "foo"
    assert parsed["dry_run"] is True
    assert parsed["aborted"] is True
    assert parsed["summary"] == "Stop foo"


def test_json_yes_emits_payload_and_returns():
    _output.set_json_mode(True)
    fake = StringIO()
    with patch("sys.stdout", fake):
        _safety.confirm_or_exit(
            summary="Stop foo",
            items=["podman stop foo"],
            yes=True,
        )
    parsed = json.loads(fake.getvalue())
    assert parsed["dry_run"] is False
    assert "error" not in parsed


def test_json_without_yes_or_dry_run_errors():
    """JSON mode can't prompt; missing --yes is invalid args (exit 2)."""
    _output.set_json_mode(True)
    fake = StringIO()
    with patch("sys.stdout", fake), pytest.raises(typer.Exit) as e:
        _safety.confirm_or_exit(summary="Stop foo", items=["podman stop foo"])
    assert e.value.exit_code == 2
    parsed = json.loads(fake.getvalue())
    assert "error" in parsed
    assert "yes" in parsed["error"].lower()
