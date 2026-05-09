"""Thin wrapper for podman calls. Builds run-args from a manifest + host context."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gpu import GpuKind, detect_gpu
from .manifest import OmnilabManifest


@dataclass
class HostContext:
    """Host-side facts that influence container launch."""

    gpu: GpuKind
    wayland_display: str | None  # path to host's wayland socket, or None
    project_dir: Path  # the directory containing omnilab.yaml


def has_podman() -> bool:
    return shutil.which("podman") is not None


def detect_host_context(project_dir: Path) -> HostContext:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    wayland = None
    if runtime_dir:
        wd = Path(runtime_dir) / "wayland-0"
        if wd.exists():
            wayland = str(wd)

    return HostContext(
        gpu=detect_gpu(),
        wayland_display=wayland,
        project_dir=project_dir.resolve(),
    )


def build_run_args(manifest: OmnilabManifest, ctx: HostContext, *, detach: bool = True) -> list[str]:
    """Construct `podman run …` arguments from manifest + host context.

    Pure function — easy to unit-test without invoking podman.
    """
    args: list[str] = ["podman", "run"]

    if detach:
        args.append("-d")

    # Stable name + label so omnilab can find/stop the container later.
    args += ["--name", manifest.name]
    args += ["--label", f"omnilab.project={manifest.name}"]

    # ROS 2 DDS multicast/discovery wants host networking; --network host
    # is the simplest path. Phase B.future may switch to a custom bridge.
    args += ["--network", "host"]

    # GPU passthrough.
    if ctx.gpu == "nvidia":
        # nvidia-container-toolkit CDI selector. Host must have the
        # toolkit installed; Phase B.5 wires this into the host image.
        args += ["--device", "nvidia.com/gpu=all"]
    elif ctx.gpu == "igpu":
        # Pass /dev/dri for KMS / DRI3.
        args += ["--device", "/dev/dri"]
    # 'none' → no GPU args; container runs without acceleration.

    # Wayland display passthrough so Gazebo / RViz / Konsole render on
    # the host desktop. v0 mounts the host socket at /tmp/wayland-0.
    if ctx.wayland_display:
        args += ["-v", f"{ctx.wayland_display}:/tmp/wayland-0"]
        args += ["-e", "WAYLAND_DISPLAY=/tmp/wayland-0"]
        args += ["-e", "XDG_RUNTIME_DIR=/tmp"]

    # Mount the project directory as /workspace inside the container.
    args += ["-v", f"{ctx.project_dir}:/workspace"]
    args += ["-w", "/workspace"]

    # ROS env from manifest.
    args += ["-e", f"RMW_IMPLEMENTATION={manifest.ros.rmw}"]
    args += ["-e", f"ROS_DOMAIN_ID={manifest.ros.domain_id}"]

    # Image at the end.
    args.append(manifest.image)

    # Default to bash so the container stays alive.
    args.append("/bin/bash")
    args += ["-c", "tail -f /dev/null"]

    return args


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Invoke podman (or any subprocess) and capture output."""
    return subprocess.run(args, check=False, capture_output=True, text=True)


def container_running(name: str) -> bool:
    """Is a container with this name currently running?"""
    if not has_podman():
        return False
    result = run(["podman", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"])
    return name in result.stdout.split()


def stop_container(name: str) -> subprocess.CompletedProcess[str]:
    """Stop a running container; tolerate 'not found'."""
    return run(["podman", "stop", name])


def exec_in(name: str, command: list[str]) -> int:
    """Exec a command inside a running container; stream output."""
    full = ["podman", "exec", "-it", name, *command]
    return subprocess.call(full)
