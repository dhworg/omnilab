"""Tests for omnilab._output — dual-mode output helpers."""

from __future__ import annotations

import json
from io import StringIO
from unittest.mock import patch

import pytest
import typer

from omnilab import _output


@pytest.fixture(autouse=True)
def reset_mode():
    _output.set_json_mode(False)
    yield
    _output.set_json_mode(False)


def test_default_mode_is_human():
    assert not _output.is_json_mode()


def test_set_json_mode_toggles():
    _output.set_json_mode(True)
    assert _output.is_json_mode()
    _output.set_json_mode(False)
    assert not _output.is_json_mode()


def test_emit_human_prints_text(capsys):
    _output.emit(human="hello")
    out = capsys.readouterr().out
    assert "hello" in out


def test_emit_human_silent_when_no_text(capsys):
    _output.emit()
    assert capsys.readouterr().out == ""


def test_emit_json_prints_data():
    _output.set_json_mode(True)
    fake = StringIO()
    with patch("sys.stdout", fake):
        _output.emit(data={"k": "v", "n": 1})
    parsed = json.loads(fake.getvalue())
    assert parsed == {"k": "v", "n": 1}


def test_emit_json_empty_when_no_data():
    _output.set_json_mode(True)
    fake = StringIO()
    with patch("sys.stdout", fake):
        _output.emit()
    parsed = json.loads(fake.getvalue())
    assert parsed == {}


def test_emit_json_ignores_human_arg():
    _output.set_json_mode(True)
    fake = StringIO()
    with patch("sys.stdout", fake):
        _output.emit(human="should not appear", data={"x": 1})
    out = fake.getvalue()
    assert "should not appear" not in out
    assert json.loads(out) == {"x": 1}


def test_emit_error_human_writes_to_stderr_and_exits(capsys):
    with pytest.raises(typer.Exit) as excinfo:
        _output.emit_error("boom", code=3)
    assert excinfo.value.exit_code == 3
    err = capsys.readouterr().err
    assert "ERROR: boom" in err


def test_emit_error_json_writes_structured_payload(capsys):
    _output.set_json_mode(True)
    with pytest.raises(typer.Exit) as excinfo:
        _output.emit_error("nope", code=4, hint="check network")
    assert excinfo.value.exit_code == 4
    err = capsys.readouterr().err
    parsed = json.loads(err)
    assert parsed["error"] == "nope"
    assert parsed["code"] == 4
    assert parsed["hint"] == "check network"


def test_style_helpers_are_string():
    # In human mode, they're styled with ANSI codes; in JSON mode, plain text.
    _output.set_json_mode(False)
    assert isinstance(_output.style_pass(), str)
    assert isinstance(_output.style_fail(), str)
    _output.set_json_mode(True)
    assert _output.style_pass() == "PASS"
    assert _output.style_fail() == "FAIL"
