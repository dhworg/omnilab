"""Tests for omnilab.manifest.OmnilabManifest — the schema parser/validator."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnilab.manifest import GpuMode, OmnilabManifest


def test_minimal_manifest_round_trips():
    m = OmnilabManifest(name="my-project", image="ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest")
    assert m.name == "my-project"
    assert m.image == "ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest"
    # Defaults from spec § "v1 must-do" #6
    assert m.ros.rmw == "rmw_cyclonedds_cpp"
    assert m.ros.domain_id == 42
    assert m.gazebo.defaults.camera_resolution == (320, 240)
    assert m.gazebo.defaults.camera_fps == 15
    assert m.gazebo.defaults.shadows is False
    assert m.gpu == "auto"
    assert m.hardware.micro_ros == "enabled"
    assert m.skills == []


def test_full_manifest_from_spec_example():
    """The example from project-spec-v1.md § Manifest schema."""
    yaml_text = textwrap.dedent(
        """
        name: my-project
        host_min_version: 0.1.0
        image: ghcr.io/dhworg/ros-jazzy-gz-harmonic@sha256:abc123def456789012345678901234567890abc123def456789012345678901a
        ros:
          rmw: rmw_cyclonedds_cpp
          domain_id: 42
        gazebo:
          default_world: turtlebot3_world.sdf
          defaults:
            shadows: false
            camera_fps: 15
            camera_resolution: [320, 240]
        gpu: auto
        hardware:
          micro_ros: enabled
          boards: [arduino_uno, esp32, stm32_blue_pill, rp2040]
        skills: []
        """
    )
    data = yaml.safe_load(yaml_text)
    m = OmnilabManifest.model_validate(data)
    assert m.name == "my-project"
    assert m.gazebo.default_world == "turtlebot3_world.sdf"
    assert m.hardware.boards == ["arduino_uno", "esp32", "stm32_blue_pill", "rp2040"]
    assert "@sha256:" in m.image


def test_from_yaml_file(tmp_path: Path):
    p = tmp_path / "omnilab.yaml"
    p.write_text(
        textwrap.dedent(
            """
            name: file-test
            image: ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest
            """
        )
    )
    m = OmnilabManifest.from_yaml(p)
    assert m.name == "file-test"


@pytest.mark.parametrize(
    "name",
    ["my-project", "my_project", "p1", "P1", "robot-arm-v2", "a"],
)
def test_valid_names(name: str):
    OmnilabManifest(name=name, image="x:y")  # should not raise


@pytest.mark.parametrize(
    "name",
    ["", "my project", "my.project", "my/project", "-leading-dash", "name!", "café"],
)
def test_invalid_names(name: str):
    with pytest.raises(ValidationError):
        OmnilabManifest(name=name, image="x:y")


def test_image_must_be_tag_or_digest_form():
    OmnilabManifest(name="t", image="ghcr.io/dhworg/x:latest")
    OmnilabManifest(name="t", image="ghcr.io/dhworg/x@sha256:abc")
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="ghcr.io/dhworg/x")  # no tag, no digest
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="")


@pytest.mark.parametrize("mode", ["auto", "igpu", "nvidia"])
def test_valid_gpu_modes(mode: GpuMode):
    OmnilabManifest(name="t", image="x:y", gpu=mode)


def test_invalid_gpu_mode_rejected():
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="x:y", gpu="amd")  # type: ignore[arg-type]


def test_extra_top_level_field_rejected():
    """`extra="forbid"` catches typos like `gpus:` instead of `gpu:`."""
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="x:y", gpus="auto")  # type: ignore[call-arg]


def test_domain_id_bounds():
    OmnilabManifest(name="t", image="x:y", ros={"rmw": "rmw_cyclonedds_cpp", "domain_id": 0})
    OmnilabManifest(name="t", image="x:y", ros={"rmw": "rmw_cyclonedds_cpp", "domain_id": 232})
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="x:y", ros={"rmw": "x", "domain_id": -1})
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="x:y", ros={"rmw": "x", "domain_id": 1000})


def test_micro_ros_literal():
    OmnilabManifest(name="t", image="x:y", hardware={"micro_ros": "disabled"})
    with pytest.raises(ValidationError):
        OmnilabManifest(name="t", image="x:y", hardware={"micro_ros": "off"})  # type: ignore[arg-type]
