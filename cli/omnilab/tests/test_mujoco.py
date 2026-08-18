"""Tests for MuJoCo support (spec amendment 2026-08-18).

The design principle under test: everything OmniLab does at the ROS layer
(observe Layer 1, tune, record, pair) is simulator-agnostic; only the
edges dispatch on `manifest.simulator` — sim launch, inspect's sim panel,
clean's process patterns, and observe's capture gate.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

from omnilab.clean import ContainerInfo, plan_cleanup
from omnilab.cli import sim_launch_command
from omnilab.inspect import parse_ros_clock_echo
from omnilab.manifest import OmnilabManifest

IMAGE = "ghcr.io/dhworg/ros-jazzy-mujoco:latest"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _manifest(**overrides) -> OmnilabManifest:
    base = {"name": "pend", "image": IMAGE}
    base.update(overrides)
    return OmnilabManifest.model_validate(base)


# ---- manifest -------------------------------------------------------------


def test_simulator_defaults_to_gazebo():
    assert _manifest().simulator == "gazebo"


def test_simulator_mujoco_accepted_with_config():
    m = _manifest(
        simulator="mujoco",
        mujoco={"model": "sim/pendulum.xml", "bridge": "sim/mujoco_bridge.py"},
    )
    assert m.simulator == "mujoco"
    assert m.mujoco.model == "sim/pendulum.xml"
    assert m.mujoco.bridge == "sim/mujoco_bridge.py"


def test_unknown_simulator_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _manifest(simulator="unity")


def test_gazebo_block_still_valid_alongside_simulator_field():
    """Back-compat: every pre-amendment manifest parses unchanged."""
    m = _manifest(gazebo={"default_world": "x.sdf"})
    assert m.simulator == "gazebo"
    assert m.gazebo.default_world == "x.sdf"


# ---- sim launch dispatch ----------------------------------------------------


def test_gazebo_launch_unchanged():
    line = sim_launch_command(_manifest())
    assert "nav2_bringup tb3_simulation_launch.py" in line
    assert "headless" not in line


def test_gazebo_headless_flag():
    assert "headless:=True" in sim_launch_command(_manifest(), headless=True)


def test_mujoco_bridge_launch():
    m = _manifest(
        simulator="mujoco",
        mujoco={"model": "sim/pendulum.xml", "bridge": "sim/mujoco_bridge.py"},
    )
    line = sim_launch_command(m)
    # ROS must be sourced (the bridge imports rclpy), paths are /workspace-
    # relative because that's where the project dir is mounted.
    assert "source /opt/ros/jazzy/setup.bash" in line
    assert "python3 /workspace/sim/mujoco_bridge.py" in line
    assert "--model /workspace/sim/pendulum.xml" in line
    assert "--headless" not in line
    assert "--headless" in sim_launch_command(m, headless=True)


def test_mujoco_viewer_fallback_without_bridge():
    m = _manifest(simulator="mujoco", mujoco={"model": "sim/pendulum.xml"})
    line = sim_launch_command(m)
    assert "python3 -m mujoco.viewer --mjcf=/workspace/sim/pendulum.xml" in line
    # The bare viewer publishes nothing, so it never needs ROS sourced.
    assert "setup.bash" not in line


def test_mujoco_without_model_is_a_manifest_error():
    with pytest.raises(ValueError, match="no model"):
        sim_launch_command(_manifest(simulator="mujoco"))


def test_mujoco_headless_without_bridge_is_rejected():
    """The bare viewer is GUI-only; headless demands a bridge."""
    m = _manifest(simulator="mujoco", mujoco={"model": "sim/pendulum.xml"})
    with pytest.raises(ValueError, match="bridge"):
        sim_launch_command(m, headless=True)


# ---- clean patterns ---------------------------------------------------------


def test_clean_reaps_duplicate_mujoco_bridges():
    plan = plan_cleanup(
        project="p",
        containers=[
            ContainerInfo(
                name="p", project="p", state="running",
                inside_procs=[
                    (100, "python3 /workspace/sim/mujoco_bridge.py --model x.xml"),
                    (200, "python3 /workspace/sim/mujoco_bridge.py --model x.xml"),
                ],
            )
        ],
        procs=[],
    )
    assert [a.target for a in plan.actions] == ["p:200"]
    assert "mujoco ROS bridge" in plan.actions[0].reason


def test_clean_single_mujoco_viewer_left_alone():
    plan = plan_cleanup(
        project="p",
        containers=[
            ContainerInfo(
                name="p", project="p", state="running",
                inside_procs=[(100, "python3 -m mujoco.viewer --mjcf=/workspace/m.xml")],
            )
        ],
        procs=[],
    )
    assert plan.actions == []


# ---- inspect: sim state via /clock ------------------------------------------


def test_parse_ros_clock_echo():
    out = "clock:\n  sec: 12\n  nanosec: 340000000\n---\n"
    assert parse_ros_clock_echo(out) == pytest.approx(12.34)


def test_parse_ros_clock_echo_no_clock():
    assert parse_ros_clock_echo("WARNING: topic [/clock] does not appear to be published yet\n") is None


# ---- templates ---------------------------------------------------------------


def test_embedded_mujoco_template_renders_to_valid_manifest():
    text = resources.files("omnilab.templates").joinpath("mujoco.yaml").read_text()
    rendered = text.replace("{name}", "my-pend")  # same mechanism as `omnilab new`
    m = OmnilabManifest.model_validate(yaml.safe_load(rendered))
    assert m.name == "my-pend"
    assert m.simulator == "mujoco"
    assert m.image.startswith("ghcr.io/dhworg/ros-jazzy-mujoco")
    assert m.mujoco.model is not None


def test_repo_template_files_all_exist():
    tdir = REPO_ROOT / "templates" / "mujoco-pendulum"
    info = yaml.safe_load((tdir / "template.yaml").read_text())
    for rel in info["files"]:
        assert (tdir / "files" / rel).exists(), f"missing {rel}"


def test_repo_template_manifest_renders_and_validates():
    from omnilab.template import render

    tdir = REPO_ROOT / "templates" / "mujoco-pendulum"
    rendered = render(
        (tdir / "files" / "omnilab.yaml").read_text(), {"project_name": "demo"}
    )
    m = OmnilabManifest.model_validate(yaml.safe_load(rendered))
    assert m.simulator == "mujoco"
    assert m.mujoco.bridge == "sim/mujoco_bridge.py"
    assert m.observers == "observers.yaml"


def test_repo_template_observers_lint_clean():
    from omnilab.observe import validate_observers

    tdir = REPO_ROOT / "templates" / "mujoco-pendulum"
    issues = validate_observers((tdir / "files" / "observers.yaml").read_text())
    assert [i for i in issues if i.level == "error"] == []
