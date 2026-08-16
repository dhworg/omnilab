"""Thin wrapper for podman calls. Builds run-args from a manifest + host context."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gpu import GpuKind, detect_gpu
from .gpu_doctor import PRIME_ENV
from .manifest import OmnilabManifest


@dataclass
class HostContext:
    """Host-side facts that influence container launch."""

    gpu: GpuKind
    wayland_display: str | None  # path to host's wayland socket, or None
    project_dir: Path  # the directory containing omnilab.yaml
    # Xwayland fallback — default None so callers that don't care about X11
    # (and every existing test) can omit them.
    x11_socket_dir: str | None = None  # host path containing X0 / X1 etc.
    x11_display: str | None = None     # ":0" / ":1" — to set DISPLAY in container
    x11_auth_file: str | None = None   # path to Xauthority file


def has_podman() -> bool:
    return shutil.which("podman") is not None


def _detect_xwayland() -> tuple[str | None, str | None, str | None]:
    """Find Xwayland on host: socket dir, DISPLAY value, Xauthority file.

    KDE Plasma 6 runs Xwayland for X11-app compat. The Xauthority file
    has a randomised suffix per session, so we scan /run/user/<uid>/.
    """
    socket_dir = "/tmp/.X11-unix"
    if not Path(socket_dir).is_dir():
        return None, None, None
    # Find display by socket name (X0, X1...)
    display = None
    for entry in Path(socket_dir).iterdir():
        if entry.name.startswith("X") and entry.name[1:].isdigit():
            display = ":" + entry.name[1:]
            break
    if display is None:
        return None, None, None
    # Find Xauthority file in /run/user/<uid>/
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    auth = None
    if runtime_dir and Path(runtime_dir).is_dir():
        for f in Path(runtime_dir).iterdir():
            if f.name.startswith("xauth"):
                auth = str(f)
                break
    return socket_dir, display, auth


def detect_host_context(project_dir: Path) -> HostContext:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    wayland = None
    if runtime_dir:
        wd = Path(runtime_dir) / "wayland-0"
        if wd.exists():
            wayland = str(wd)

    x11_dir, x11_disp, x11_auth = _detect_xwayland()

    return HostContext(
        gpu=detect_gpu(),
        wayland_display=wayland,
        x11_socket_dir=x11_dir,
        x11_display=x11_disp,
        x11_auth_file=x11_auth,
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
    # --replace removes any same-named stopped container so omnilab up
    # is idempotent even when omnilab down left a dead one behind.
    args += ["--replace"]
    args += ["--name", manifest.name]
    args += ["--label", f"omnilab.project={manifest.name}"]

    # Map host UID/GID into the container so files written under /workspace
    # are owned by the host user (parth), not the rootless-podman remap.
    # Without this, `colcon build` inside the container can't write
    # build/install/log in the bind-mounted ros2_ws.
    args += ["--userns", "keep-id"]

    # On a Fedora bootc host with SELinux enforcing, the default
    # container_t domain can't connect to a `user_runtime_t` Wayland
    # socket. Disable label confinement for this container — safe
    # because it's already userns-restricted to the invoking user.
    args += ["--security-opt", "label=disable"]

    # ROS 2 DDS multicast/discovery wants host networking; --network host
    # is the simplest path. Phase B.future may switch to a custom bridge.
    args += ["--network", "host"]

    # GPU passthrough.
    if ctx.gpu == "nvidia":
        # nvidia-container-toolkit CDI selector. Host must have the
        # toolkit installed; `omnilab gpu` diagnoses when it isn't.
        args += ["--device", "nvidia.com/gpu=all"]
        # PRIME render offload. Without these, passthrough succeeds and the
        # GL/Vulkan stack still picks the iGPU (or llvmpipe) on every hybrid
        # laptop — the GPU is present, awake, and completely unused. This is
        # the single most common "my dGPU isn't working" report.
        for key, value in PRIME_ENV.items():
            args += ["-e", f"{key}={value}"]
    elif ctx.gpu == "igpu":
        # Pass /dev/dri for KMS / DRI3.
        args += ["--device", "/dev/dri"]
    # 'none' → no GPU args; container runs without acceleration.

    # Wayland display passthrough so Gazebo / RViz / Konsole render on
    # the host desktop. v0 mounts the host socket at /tmp/wayland-0.
    if ctx.wayland_display:
        args += ["-v", f"{ctx.wayland_display}:/tmp/wayland-0"]
        # Qt's wayland plugin treats WAYLAND_DISPLAY as a path relative
        # to XDG_RUNTIME_DIR. Set the bare socket name so the resolved
        # path becomes /tmp/wayland-0 inside the container.
        args += ["-e", "WAYLAND_DISPLAY=wayland-0"]
        args += ["-e", "XDG_RUNTIME_DIR=/tmp"]

    # Xwayland fallback for OGRE/GLX-based renderers (Gazebo Harmonic GUI):
    # Qt5's wayland plugin can host the Qt window, but Gazebo's OGRE
    # OpenGL context creation fails on pure Wayland — EGL PBuffer doesn't
    # accept "Full Screen", and OGRE1's GLX path obviously needs X. By
    # binding the host's Xwayland socket + Xauthority into the container
    # and exposing DISPLAY, we give OGRE an X server to render against
    # while Qt itself still talks Wayland for the window chrome.
    if ctx.x11_socket_dir and ctx.x11_display:
        args += ["-v", f"{ctx.x11_socket_dir}:/tmp/.X11-unix"]
        args += ["-e", f"DISPLAY={ctx.x11_display}"]
        if ctx.x11_auth_file:
            args += ["-v", f"{ctx.x11_auth_file}:/tmp/.Xauthority:ro"]
            args += ["-e", "XAUTHORITY=/tmp/.Xauthority"]

    # Qt platform: when Xwayland is available, prefer xcb (X11) so Qt
    # and OGRE1's GLX renderer share the same X/GL stack and avoid
    # "currentGLContext was specified with no current GL context"
    # errors. Wayland-only is the fallback when Xwayland is missing.
    if ctx.x11_socket_dir:
        args += ["-e", "QT_QPA_PLATFORM=xcb;wayland"]
    elif ctx.wayland_display:
        args += ["-e", "QT_QPA_PLATFORM=wayland"]

    # Mount the project directory as /workspace inside the container.
    # `:Z` triggers SELinux relabel to container_file_t so a Fedora bootc
    # host with enforcing SELinux can grant the container access.
    args += ["-v", f"{ctx.project_dir}:/workspace:Z"]
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
