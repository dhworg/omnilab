#!/usr/bin/env python3
"""MuJoCo → ROS 2 bridge. Steps the model in real time and publishes
/clock (rosgraph_msgs/Clock) and /joint_states (sensor_msgs/JointState),
which is the contract the rest of OmniLab consumes:

  - `omnilab observe` reads /joint_states into `joints.<name>.*` for
    observers.yaml predicates,
  - `omnilab inspect` reads /clock for sim liveness + sim_time,
  - `omnilab record` bags both.

It also serves OmniLab's visual-verification protocol. Unlike Gazebo,
MuJoCo has no external capture API — but the bridge lives INSIDE the sim
process, so it can do one better: pause physics, render the current step
offscreen, and answer with the exact sim_time of the frame. State and
image are the same simulator instant by construction.

Protocol (all under <project>/.omnilab/mujoco_capture/, which is the
same directory on host and in-container because /workspace is a bind
mount — no exec channel needed):

  request.json   {"nonce": "..."}          written by `omnilab observe`
  response.json  {"nonce", "sim_time", "frame", "error"}   written here
  frame-<nonce>.png                         the rendered frame

Physics stays paused while request.json exists; observe deletes it to
resume. While paused, /clock and /joint_states keep republishing the
frozen values so observe's collect window sees them.

Launched by `omnilab sim` as:
    python3 sim/mujoco_bridge.py --model sim/pendulum.xml [--headless]
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# Offscreen rendering backend: respect an explicit MUJOCO_GL; otherwise
# default to osmesa when there is no display (CPU render, always works —
# it's what the image's smoke test proves). With a display, glfw is fine
# for both the viewer and the renderer.
if "MUJOCO_GL" not in os.environ and not (
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
):
    os.environ["MUJOCO_GL"] = "osmesa"

import mujoco
import rclpy
from rclpy.node import Node
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import JointState

PUBLISH_HZ = 50.0
CAPTURE_DIR = Path(".omnilab/mujoco_capture")
FRAME_SIZE = (480, 480)  # (height, width)


class MujocoBridge(Node):
    def __init__(self, model: mujoco.MjModel, data: mujoco.MjData) -> None:
        super().__init__("mujoco_bridge")
        self.model = model
        self.data = data
        self.clock_pub = self.create_publisher(Clock, "/clock", 10)
        self.joints_pub = self.create_publisher(JointState, "/joint_states", 10)
        # Joint names once, in qpos order. Free joints have no scalar
        # position/velocity pair; publish hinge/slide joints only.
        self.joint_ids = [
            j for j in range(model.njnt)
            if model.jnt_type[j] in (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE)
        ]
        self.joint_names = [
            mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, j) or f"joint_{j}"
            for j in self.joint_ids
        ]

    def publish(self) -> None:
        t = self.data.time
        clock = Clock()
        clock.clock.sec = int(t)
        clock.clock.nanosec = int((t - int(t)) * 1e9)
        self.clock_pub.publish(clock)

        js = JointState()
        js.header.stamp = clock.clock
        js.name = self.joint_names
        js.position = [float(self.data.qpos[self.model.jnt_qposadr[j]]) for j in self.joint_ids]
        js.velocity = [float(self.data.qvel[self.model.jnt_dofadr[j]]) for j in self.joint_ids]
        js.effort = [0.0] * len(self.joint_ids)
        self.joints_pub.publish(js)


class CaptureServer:
    """File-based capture protocol. Renderer is lazy so a broken GL stack
    degrades to error responses instead of killing the bridge."""

    def __init__(self, model: mujoco.MjModel, data: mujoco.MjData, capture_dir: Path) -> None:
        self.model = model
        self.data = data
        self.dir = capture_dir
        self._renderer: "mujoco.Renderer | None" = None
        self._renderer_error: str | None = None
        self._served_nonce: str | None = None

    def _get_renderer(self):
        if self._renderer is None and self._renderer_error is None:
            try:
                self._renderer = mujoco.Renderer(self.model, height=FRAME_SIZE[0], width=FRAME_SIZE[1])
            except Exception as e:  # noqa: BLE001 — report, don't crash the sim
                self._renderer_error = f"renderer init failed ({os.environ.get('MUJOCO_GL', 'default')}): {e}"
        return self._renderer

    def pending_request(self) -> str | None:
        """Nonce of the current request, or None. Malformed files count as
        a request (physics must still pause) but get an error response."""
        req = self.dir / "request.json"
        if not req.exists():
            return None
        try:
            return str(json.loads(req.read_text())["nonce"])
        except Exception:  # noqa: BLE001
            return "malformed"

    def serve(self, nonce: str) -> None:
        if nonce == self._served_nonce:
            return
        self._served_nonce = nonce
        resp: dict = {"nonce": nonce, "sim_time": float(self.data.time), "frame": None, "error": None}
        if nonce == "malformed":
            resp["error"] = "request.json was not valid JSON"
        else:
            r = self._get_renderer()
            if r is None:
                resp["error"] = self._renderer_error
            else:
                try:
                    r.update_scene(self.data)
                    pixels = r.render()
                    frame = self.dir / f"frame-{nonce}.png"
                    _write_png(frame, pixels)
                    resp["frame"] = frame.name
                except Exception as e:  # noqa: BLE001
                    resp["error"] = f"render failed: {e}"
        tmp = self.dir / "response.json.tmp"
        tmp.write_text(json.dumps(resp))
        os.replace(tmp, self.dir / "response.json")


def _write_png(path: Path, pixels) -> None:
    """Minimal dependency-free PNG writer (RGB8). MuJoCo returns HxWx3
    uint8; zlib + struct are stdlib, so no imaging library is needed."""
    import struct
    import zlib

    h, w, _ = pixels.shape
    raw = b"".join(b"\x00" + pixels[y].tobytes() for y in range(h))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(png)
    os.replace(tmp, path)


def run(model_path: str, *, headless: bool) -> int:
    model = mujoco.MjModel.from_xml_path(model_path)
    data = mujoco.MjData(model)
    if model.nkey > 0:  # start from the first keyframe when one exists
        mujoco.mj_resetDataKeyframe(model, data, 0)

    capture_dir = CAPTURE_DIR
    capture_dir.mkdir(parents=True, exist_ok=True)
    # A stale request from a dead observe run would pause us at boot.
    (capture_dir / "request.json").unlink(missing_ok=True)

    rclpy.init()
    node = MujocoBridge(model, data)
    server = CaptureServer(model, data, capture_dir)
    node.get_logger().info(
        f"bridging {model_path}: joints={node.joint_names}, publish={PUBLISH_HZ}Hz, "
        f"capture={capture_dir}/"
    )

    viewer_ctx = None
    if not headless:
        try:
            import mujoco.viewer

            viewer_ctx = mujoco.viewer.launch_passive(model, data)
        except Exception as e:  # noqa: BLE001 — no display is normal, not fatal
            node.get_logger().warning(f"viewer unavailable ({e}); continuing headless")

    publish_period = 1.0 / PUBLISH_HZ
    wall_start = time.monotonic()
    next_publish = 0.0        # sim-time schedule while running
    next_wall_publish = 0.0   # wall-time schedule while paused
    pause_started: float | None = None
    try:
        while rclpy.ok():
            nonce = server.pending_request()
            if nonce is not None:
                # Paused: physics frozen at this exact step. Serve the
                # frame once, keep republishing the frozen state so
                # observe's collect window sees it.
                if pause_started is None:
                    pause_started = time.monotonic()
                server.serve(nonce)
                now = time.monotonic()
                if now >= next_wall_publish:
                    node.publish()
                    next_wall_publish = now + publish_period
            else:
                if pause_started is not None:
                    # Resumed: shift the pacing origin so the sim doesn't
                    # fast-forward through the paused wall time.
                    wall_start += time.monotonic() - pause_started
                    pause_started = None
                wall_elapsed = time.monotonic() - wall_start
                while data.time < wall_elapsed:
                    mujoco.mj_step(model, data)
                if data.time >= next_publish:
                    node.publish()
                    next_publish = data.time + publish_period

            if viewer_ctx is not None:
                if not viewer_ctx.is_running():
                    break
                viewer_ctx.sync()
            rclpy.spin_once(node, timeout_sec=0)
            time.sleep(0.002)
    except KeyboardInterrupt:
        pass
    finally:
        if viewer_ctx is not None:
            viewer_ctx.close()
        node.destroy_node()
        rclpy.shutdown()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="MJCF model file")
    ap.add_argument("--headless", action="store_true", help="No viewer window")
    args = ap.parse_args()
    return run(args.model, headless=args.headless)


if __name__ == "__main__":
    raise SystemExit(main())
