"""End-to-end tests for `omnilab new` via Typer's CliRunner."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from omnilab.cli import app
from omnilab.manifest import OmnilabManifest


def test_new_creates_project(tmp_path: Path):
    runner = CliRunner()
    project = tmp_path / "robot-foo"
    result = runner.invoke(app, ["new", "robot-foo", "--directory", str(project)])
    assert result.exit_code == 0, result.stdout
    assert project.exists()
    yaml_path = project / "omnilab.yaml"
    assert yaml_path.exists()

    # The generated file must round-trip through the manifest validator.
    m = OmnilabManifest.from_yaml(yaml_path)
    assert m.name == "robot-foo"
    assert "ros-jazzy-gz-harmonic" in m.image


def test_new_refuses_to_overwrite(tmp_path: Path):
    runner = CliRunner()
    project = tmp_path / "x"
    project.mkdir()
    result = runner.invoke(app, ["new", "x", "--directory", str(project)])
    assert result.exit_code != 0
    # CliRunner.output combines stdout + stderr; the "already exists" line
    # we emit via typer.echo(..., err=True) lands here.
    assert "already exists" in result.output


def test_new_with_invalid_name(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["new", "bad name!", "--directory", str(tmp_path / "x")])
    assert result.exit_code != 0


def test_new_with_unknown_template(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["new", "ok-name", "--template", "no-such-template", "--directory", str(tmp_path / "x")],
    )
    assert result.exit_code != 0


def test_generated_yaml_is_well_formed(tmp_path: Path):
    runner = CliRunner()
    project = tmp_path / "yamlcheck"
    runner.invoke(app, ["new", "yamlcheck", "--directory", str(project)])
    raw = (project / "omnilab.yaml").read_text()
    parsed = yaml.safe_load(raw)
    assert parsed["name"] == "yamlcheck"
    assert "image" in parsed
    assert "ros" in parsed
    assert parsed["gpu"] == "auto"
