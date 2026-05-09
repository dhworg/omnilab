"""Tests for podman.build_run_args — pure function, easy to verify."""

from __future__ import annotations

from pathlib import Path

from omnilab.manifest import OmnilabManifest
from omnilab.podman import HostContext, build_run_args


def _ctx(gpu: str = "igpu", *, wayland: str | None = None, project_dir: Path | None = None) -> HostContext:
    return HostContext(
        gpu=gpu,  # type: ignore[arg-type]
        wayland_display=wayland,
        project_dir=project_dir or Path("/tmp/proj"),
    )


def _manifest(**overrides) -> OmnilabManifest:
    base = {"name": "test-proj", "image": "ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest"}
    base.update(overrides)
    return OmnilabManifest.model_validate(base)


def test_basic_args_include_image_and_name():
    args = build_run_args(_manifest(), _ctx())
    assert args[:2] == ["podman", "run"]
    assert "--name" in args
    assert args[args.index("--name") + 1] == "test-proj"
    assert "ghcr.io/dhworg/ros-jazzy-gz-harmonic:latest" in args


def test_detach_flag():
    args = build_run_args(_manifest(), _ctx(), detach=True)
    assert "-d" in args
    args = build_run_args(_manifest(), _ctx(), detach=False)
    assert "-d" not in args


def test_nvidia_passthrough():
    args = build_run_args(_manifest(), _ctx(gpu="nvidia"))
    assert "nvidia.com/gpu=all" in args


def test_igpu_passthrough():
    args = build_run_args(_manifest(), _ctx(gpu="igpu"))
    assert "/dev/dri" in args


def test_no_gpu_passthrough_when_none():
    args = build_run_args(_manifest(), _ctx(gpu="none"))
    assert "nvidia.com/gpu=all" not in args
    assert "/dev/dri" not in args


def test_wayland_socket_mounted_when_present():
    ctx = _ctx(wayland="/run/user/1000/wayland-0")
    args = build_run_args(_manifest(), ctx)
    assert "/run/user/1000/wayland-0:/tmp/wayland-0" in args
    assert "WAYLAND_DISPLAY=/tmp/wayland-0" in args


def test_wayland_skipped_when_absent():
    args = build_run_args(_manifest(), _ctx(wayland=None))
    assert not any("wayland-0" in a for a in args)


def test_rmw_and_domain_passed_via_env():
    args = build_run_args(_manifest(), _ctx())
    assert "RMW_IMPLEMENTATION=rmw_cyclonedds_cpp" in args
    assert "ROS_DOMAIN_ID=42" in args


def test_custom_rmw_propagates():
    m = _manifest(ros={"rmw": "rmw_zenoh_cpp", "domain_id": 7})
    args = build_run_args(m, _ctx())
    assert "RMW_IMPLEMENTATION=rmw_zenoh_cpp" in args
    assert "ROS_DOMAIN_ID=7" in args


def test_project_dir_mounted_at_workspace():
    ctx = _ctx(project_dir=Path("/home/parth/projects/foo"))
    args = build_run_args(_manifest(), ctx)
    assert "/home/parth/projects/foo:/workspace" in args
    assert "-w" in args
    assert args[args.index("-w") + 1] == "/workspace"


def test_host_network_for_ros_dds():
    args = build_run_args(_manifest(), _ctx())
    assert "--network" in args
    assert args[args.index("--network") + 1] == "host"
