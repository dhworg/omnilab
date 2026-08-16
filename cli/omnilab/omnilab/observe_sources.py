"""Live-state source for `omnilab observe` — bridges the project's running
container into the predicate engine.

Per project-spec-v1.md (rev 3) § "Pillar: Agent perception":
  Layer 1 spatial summary was previously evaluated against a canned
  example state (`example_quadruped_state()`). This module is the
  in-spec implementation of `PodmanExecSources` (referenced as
  Phase B.future in the original observe.py — now landing earlier).

Mechanism:
  podman exec <container> python3 - < <rclpy-snapshot-script>
where the script spins up a one-shot rclpy node, subscribes to
/odom, /imu, /joint_states, /tf, /tf_static, /clock, /model/.../pose
plus optional gz contact topics, collects ONE message per topic during
a short window (default 2.0 s), and prints a JSON snapshot to stdout.

The CLI parses the JSON, runs the contract-defined mapping (joints +
pose + contacts + is_upright + tf_frames staleness), and passes the
result into ObserversEngine.tick().

Why a python3 stdin script vs four separate `ros2 topic echo --once`
calls:
  * One round-trip → one timeout to manage, not four
  * Atomic snapshot — all topics sampled in the same 2 s window
  * Easier to mock for unit tests (just stub the podman runner)
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_COLLECT_WINDOW_SECONDS = 2.0
DEFAULT_PODMAN_TIMEOUT_SECONDS = 8.0

# Topics the snapshot script subscribes to. The exact set is sniffed
# at runtime — missing topics show up in topics_missing, not as errors.
DEFAULT_TOPIC_SET = (
    "/odom",
    "/imu/data",
    "/joint_states",
    "/tf",
    "/tf_static",
    "/clock",
    "/model/quadruped/pose",  # gz dynamic_pose bridge default name
)


# ---- rclpy snapshot script (executed inside the project container) -------

_SNAPSHOT_SCRIPT = r"""
import json
import sys
import time
import traceback

try:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import JointState, Imu
    from geometry_msgs.msg import PoseArray
    from nav_msgs.msg import Odometry
    from rosgraph_msgs.msg import Clock
    from tf2_msgs.msg import TFMessage
except Exception as e:
    print(json.dumps({"error": f"rclpy import failed: {e}"}))
    sys.exit(0)


def _pose_to_dict(p):
    return {
        "position": [p.position.x, p.position.y, p.position.z],
        "orientation_quat": [
            p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w,
        ],
    }


class Snapshot(Node):
    def __init__(self):
        super().__init__("omnilab_observe_snapshot")
        self.data = {
            "odom": None,
            "imu": None,
            "joint_state": None,
            "pose_array": None,
            "clock": None,
            "tf": [],
            "tf_static": [],
            "received": set(),
        }
        qos = 10
        # rclpy inspects callable signatures; bound methods sometimes
        # confuse it, so wrap in lambdas which always look like 1-arg.
        # Also: Node base sets self._clock to a ROSClock instance, so we
        # must NOT name our handlers _clock / _odom / etc. that collide
        # with base attributes. Use _cb_* prefix to be safe.
        self.create_subscription(Odometry, "/odom", lambda m: self._cb_odom(m), qos)
        self.create_subscription(Imu, "/imu/data", lambda m: self._cb_imu(m), qos)
        self.create_subscription(JointState, "/joint_states", lambda m: self._cb_js(m), qos)
        self.create_subscription(PoseArray, "/model/quadruped/pose", lambda m: self._cb_pose(m), qos)
        self.create_subscription(Clock, "/clock", lambda m: self._cb_clock(m), qos)
        self.create_subscription(TFMessage, "/tf", lambda m: self._cb_tf(m), qos)
        self.create_subscription(TFMessage, "/tf_static", lambda m: self._cb_tfs(m), qos)

    def _cb_odom(self, m):
        self.data["odom"] = {
            "stamp": m.header.stamp.sec + m.header.stamp.nanosec / 1e9,
            "frame_id": m.header.frame_id,
            "child_frame_id": m.child_frame_id,
            "pose": _pose_to_dict(m.pose.pose),
            "twist_linear": [m.twist.twist.linear.x, m.twist.twist.linear.y, m.twist.twist.linear.z],
            "twist_angular": [m.twist.twist.angular.x, m.twist.twist.angular.y, m.twist.twist.angular.z],
        }
        self.data["received"].add("/odom")

    def _cb_imu(self, m):
        self.data["imu"] = {
            "stamp": m.header.stamp.sec + m.header.stamp.nanosec / 1e9,
            "orientation_quat": [m.orientation.x, m.orientation.y, m.orientation.z, m.orientation.w],
            "angular_velocity": [m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z],
            "linear_acceleration": [m.linear_acceleration.x, m.linear_acceleration.y, m.linear_acceleration.z],
        }
        self.data["received"].add("/imu/data")

    def _cb_js(self, m):
        self.data["joint_state"] = {
            "stamp": m.header.stamp.sec + m.header.stamp.nanosec / 1e9,
            "name": list(m.name),
            "position": list(m.position),
            "velocity": list(m.velocity),
            "effort": list(m.effort),
        }
        self.data["received"].add("/joint_states")

    def _cb_pose(self, m):
        self.data["pose_array"] = {
            "stamp": m.header.stamp.sec + m.header.stamp.nanosec / 1e9,
            "frame_id": m.header.frame_id,
            "poses": [_pose_to_dict(p) for p in m.poses],
        }
        self.data["received"].add("/model/quadruped/pose")

    def _cb_clock(self, m):
        self.data["clock"] = m.clock.sec + m.clock.nanosec / 1e9
        self.data["received"].add("/clock")

    def _cb_tf(self, m):
        for t in m.transforms:
            self.data["tf"].append({
                "parent": t.header.frame_id,
                "child": t.child_frame_id,
                "stamp": t.header.stamp.sec + t.header.stamp.nanosec / 1e9,
                "translation": [t.transform.translation.x, t.transform.translation.y, t.transform.translation.z],
                "rotation": [t.transform.rotation.x, t.transform.rotation.y, t.transform.rotation.z, t.transform.rotation.w],
            })
        self.data["received"].add("/tf")

    def _cb_tfs(self, m):
        for t in m.transforms:
            self.data["tf_static"].append({
                "parent": t.header.frame_id,
                "child": t.child_frame_id,
                "stamp": t.header.stamp.sec + t.header.stamp.nanosec / 1e9,
            })
        self.data["received"].add("/tf_static")


def main():
    try:
        window_s = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
        rclpy.init(args=None)
        n = Snapshot()
        end = time.time() + window_s
        while time.time() < end:
            rclpy.spin_once(n, timeout_sec=0.05)
        out = dict(n.data)
        out["received"] = sorted(list(out["received"]))
        out["wall_now"] = time.time()
        print(json.dumps(out, default=str))
        n.destroy_node()
        rclpy.shutdown()
    except Exception as e:
        print(json.dumps({"error": str(e), "traceback": traceback.format_exc()}))


if __name__ == "__main__":
    main()
"""


# ---- container state -----------------------------------------------------


@dataclass
class ContainerStatus:
    name: str
    state: str  # "running", "exited", "paused", "created", "missing"
    image: str | None


def _has_podman() -> bool:
    return shutil.which("podman") is not None


def detect_container(name: str, runner: Callable[..., subprocess.CompletedProcess] | None = None) -> ContainerStatus:
    """Look up a container by name. Returns ContainerStatus.state="missing"
    if the container is not present at all.
    """
    run = runner or _default_runner
    r = run(
        ["podman", "container", "inspect", "--format",
         "{{.State.Status}}|{{.Config.Image}}", name],
        capture_output=True, text=True, check=False, timeout=5.0,
    )
    if r.returncode != 0:
        return ContainerStatus(name=name, state="missing", image=None)
    parts = (r.stdout or "").strip().split("|", 1)
    state = parts[0] if parts else "unknown"
    image = parts[1] if len(parts) > 1 else None
    return ContainerStatus(name=name, state=state, image=image)


def _default_runner(args, **kw):  # split out for monkeypatching in tests
    return subprocess.run(args, **kw)


# ---- live source ---------------------------------------------------------


@dataclass
class SampleResult:
    """What LiveStateSource.sample() returns."""

    state: dict[str, Any]  # the predicate-engine-shaped dict
    topics_seen: list[str]
    topics_missing: list[str]
    collect_window_seconds: float
    state_sample_started_at: str = ""  # ISO-8601 of when the window started
    state_sample_ended_at: str = ""    # ISO-8601 of when the window ended
    raw_error: str | None = None


@dataclass
class CaptureResult:
    """What LiveStateSource.capture_screenshot() returns."""

    frame_path: str | None  # absolute host path to the PNG, or None on failure
    captured_at: str = ""   # ISO-8601 of when the screenshot was taken (wall clock)
    sim_time_s: float | None = None  # /clock value at capture instant (sim time)
    error: str | None = None


@dataclass
class CalibratedSample:
    """Output of LiveStateSource.calibrated_sample() — atomic pair of
    state + image captured at the SAME simulator instant via
    pause-capture-resume.

    Enforces feedback_calibration_simultaneity.md rule:
      derived classes are only valid if backed by an image of the
      SAME simulator instant, not "recently calibrated."

    The schema-level invariant: state_sim_time == image_sim_time
    (modulo tiny ros_gz_bridge skew). If they're more than
    sim_time_skew_tolerance_s apart, calibrated=False and the caller
    must treat all derived classes as unverified.
    """

    state_result: "SampleResult"
    capture_result: CaptureResult
    state_sim_time_s: float | None
    image_sim_time_s: float | None
    sim_time_skew_s: float | None
    calibrated: bool
    calibration_method: str  # e.g. "pause_capture_resume_v1"
    calibration_error: str | None = None


class LiveStateSource:
    """Reads one snapshot of ROS state from a running project container.

    Contract:
      - `available()` returns True iff a container with this name is
        currently in state "running". Other states (exited, paused) are
        explicitly NOT available (the caller raises exit-3).
      - `sample()` runs a single bounded rclpy snapshot inside the
        container and returns a SampleResult. Topics that don't publish
        within the collect window show up in topics_missing, not as
        exceptions. If the snapshot script itself fails (rclpy import,
        json parse), raw_error is set.
    """

    def __init__(
        self,
        container_name: str,
        *,
        collect_window_seconds: float = DEFAULT_COLLECT_WINDOW_SECONDS,
        podman_timeout_seconds: float = DEFAULT_PODMAN_TIMEOUT_SECONDS,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.container_name = container_name
        self.collect_window_seconds = collect_window_seconds
        self.podman_timeout_seconds = podman_timeout_seconds
        self._runner = runner or _default_runner

    def status(self) -> ContainerStatus:
        return detect_container(self.container_name, runner=self._runner)

    def available(self) -> bool:
        return self.status().state == "running"

    def apply_camera_pose(
        self,
        world_position_xyz: list[float],
        look_at_xyz: list[float],
    ) -> tuple[bool, str | None]:
        """Position the gz GUI main camera in world frame, pointed at a
        world-frame target. Idempotent — safe to call before every
        capture.

        Unambiguous regardless of entity rotation: a "place the camera
        at (2, -2, 1.5) looking at the bot at (0, 0, 0.2)" instruction
        produces the same view whether the bot's local Z is up,
        sideways, or upside-down. Contrast with the older
        follow+offset approach where offsets were re-interpreted in
        the entity's local frame and could land the camera under the
        floor for URDFs with non-identity spawn rotations.

        Implementation:
          1. Compute yaw + pitch from camera->target vector
          2. Convert (roll=0, pitch, yaw) to quaternion
          3. gz service /gui/move_to/pose with GUICamera{pose=...}
        """
        # Clear any active /gui/follow first — follow locks override
        # move_to/pose, so a stale follow from an earlier session would
        # keep yanking the camera back to its follow-offset position.
        clear_cmd = [
            "podman", "exec", self.container_name, "bash", "-c",
            # Empty string disables follow tracking on gz_gui.
            "gz service -s /gui/follow "
            "--reqtype gz.msgs.StringMsg --reptype gz.msgs.Boolean "
            "--timeout 1500 --req 'data: \"\"'",
        ]
        try:
            self._runner(clear_cmd, capture_output=True, text=True, check=False, timeout=4.0)
        except subprocess.TimeoutExpired:
            pass  # Non-fatal — try move_to anyway

        import math as _math
        cx, cy, cz = world_position_xyz
        tx, ty, tz = look_at_xyz
        dx, dy, dz = tx - cx, ty - cy, tz - cz
        yaw = _math.atan2(dy, dx)
        dist_xy = _math.sqrt(dx * dx + dy * dy)
        # Camera looks along its local +X axis (gz convention). Negative
        # pitch tilts the +X axis downward toward the target when the
        # target is BELOW the camera. dz = target_z - camera_z is
        # negative for "target below"; we want pitch to point the
        # camera's forward axis at the target, so pitch = atan2(dz, dist).
        pitch = _math.atan2(dz, dist_xy)
        roll = 0.0
        # RPY → quaternion (intrinsic XYZ → x,y,z,w).
        cr, sr = _math.cos(roll * 0.5), _math.sin(roll * 0.5)
        cp, sp = _math.cos(pitch * 0.5), _math.sin(pitch * 0.5)
        cyq, syq = _math.cos(yaw * 0.5), _math.sin(yaw * 0.5)
        qx = sr * cp * cyq - cr * sp * syq
        qy = cr * sp * cyq + sr * cp * syq
        qz = cr * cp * syq - sr * sp * cyq
        qw = cr * cp * cyq + sr * sp * syq

        req = (
            f"pose {{ position {{ x: {cx} y: {cy} z: {cz} }} "
            f"orientation {{ x: {qx} y: {qy} z: {qz} w: {qw} }} }}"
        )
        cmd = [
            "podman", "exec", self.container_name, "bash", "-c",
            f"gz service -s /gui/move_to/pose "
            f"--reqtype gz.msgs.GUICamera --reptype gz.msgs.Boolean "
            f"--timeout 2000 --req '{req}'",
        ]
        try:
            r = self._runner(cmd, capture_output=True, text=True, check=False, timeout=5.0)
        except subprocess.TimeoutExpired as e:
            return False, f"gui/move_to/pose timeout: {e}"
        if r.returncode != 0:
            return False, f"gui/move_to/pose rc={r.returncode}: {(r.stderr or '')[:200]}"
        return True, None

    def _world_control(self, *, pause: bool) -> tuple[bool, str | None]:
        """Pause or resume the running gz world via gz transport service.
        Returns (ok, error). The pause-capture-resume sequence in
        `calibrated_sample` depends on this — failures are surfaced so
        the caller can mark the calibration broken.
        """
        flag = "true" if pause else "false"
        cmd = [
            "podman", "exec", self.container_name, "bash", "-c",
            "gz service -s /world/default/control "
            "--reqtype gz.msgs.WorldControl --reptype gz.msgs.Boolean "
            f"--timeout 1500 --req 'pause: {flag}'",
        ]
        try:
            r = self._runner(cmd, capture_output=True, text=True, check=False, timeout=4.0)
        except subprocess.TimeoutExpired as e:
            return False, f"world_control timeout: {e}"
        if r.returncode != 0:
            return False, f"world_control rc={r.returncode}: {(r.stderr or '')[:200]}"
        # Service returns `data: true` on success
        if "data: true" not in (r.stdout or ""):
            return False, f"world_control unexpected reply: {(r.stdout or '')[:200]}"
        return True, None

    def _read_sim_clock(self) -> tuple[float | None, str | None]:
        """Read the current value of /clock (sim_time in seconds). Used
        to verify state and image were captured at the same simulator
        instant. Polls gz directly (not via the ROS bridge) to avoid
        bridge buffering / latency.
        """
        cmd = [
            "podman", "exec", self.container_name, "bash", "-c",
            # gz topic echo with -n 1 returns one message then exits.
            # /clock under gz is sim::msgs::Clock — fields sec + nsec.
            "timeout 2 gz topic -e -t /clock -n 1 2>/dev/null",
        ]
        try:
            r = self._runner(cmd, capture_output=True, text=True, check=False, timeout=4.0)
        except subprocess.TimeoutExpired as e:
            return None, f"read_sim_clock timeout: {e}"
        if r.returncode not in (0, 124):  # 124 = timeout's exit on success-with-data
            return None, f"read_sim_clock rc={r.returncode}: {(r.stderr or '')[:200]}"
        out = r.stdout or ""
        # Parse: 'sim {\n  sec: 1234\n  nsec: 567000000\n}\nreal {...'
        import re as _re
        m = _re.search(r"sim\s*\{\s*sec:\s*(\d+)\s*nsec:\s*(\d+)", out, _re.DOTALL)
        if not m:
            # Fallback: try just sec + nsec at top level (raw Clock msg)
            m2 = _re.search(r"sec:\s*(\d+)\s*nsec:\s*(\d+)", out)
            if not m2:
                return None, f"could not parse /clock output: {out[:200]}"
            sec, nsec = int(m2.group(1)), int(m2.group(2))
        else:
            sec, nsec = int(m.group(1)), int(m.group(2))
        return sec + nsec / 1e9, None

    def calibrated_sample(
        self,
        host_capture_dir: Path,
        *,
        sim_time_skew_tolerance_s: float = 0.05,
    ) -> CalibratedSample:
        """Atomic state + image capture at the same simulator instant.

        Sequence (the pause-capture-resume protocol):
          1. Read sim_time T0 from /clock
          2. Pause the world (sim_time stops advancing)
          3. Capture screenshot — rendered scene reflects sim at T0
          4. Read sim_time T1 from /clock — should equal T0 since paused
          5. Sample state (uses ROS bridge subscriptions; latest values
             reflect sim near T0 because publishers stopped at pause)
          6. Resume the world
          7. Compare T0 == T1 == state.clock; if all within tolerance,
             calibrated=True. Else calibrated=False with reason.

        Enforces feedback_calibration_simultaneity.md.
        """
        # 1. Read sim_time before pause
        t0, err0 = self._read_sim_clock()

        # 2. Pause
        paused_ok, pause_err = self._world_control(pause=True)
        if not paused_ok:
            # Couldn't pause — calibration impossible. Return whatever
            # we can but mark calibrated=False.
            sample = self.sample()
            return CalibratedSample(
                state_result=sample,
                capture_result=CaptureResult(frame_path=None, error="pause failed"),
                state_sim_time_s=None, image_sim_time_s=None,
                sim_time_skew_s=None,
                calibrated=False,
                calibration_method="pause_capture_resume_v1",
                calibration_error=f"pause failed: {pause_err}",
            )

        capture_result: CaptureResult
        sample_result: "SampleResult"
        t1: float | None = None
        t1_err: str | None = None
        try:
            # 3. Take screenshot — atomic with the paused sim_time
            capture_result = self.capture_screenshot(host_capture_dir)

            # 4. Re-read sim_time — should match t0 since paused
            t1, t1_err = self._read_sim_clock()

            # 5. Sample state. Sim is paused so the rclpy snapshot
            #    script will see only messages already queued from
            #    before pause. We use a short window because no new
            #    messages will arrive.
            saved_window = self.collect_window_seconds
            try:
                self.collect_window_seconds = 0.5
                sample_result = self.sample()
            finally:
                self.collect_window_seconds = saved_window
        finally:
            # 6. ALWAYS resume — never leave the world paused.
            self._world_control(pause=False)

        # Set sim_time on capture_result so the schema carries it
        capture_result.sim_time_s = t1 if t1 is not None else t0

        # 7. Validate the simultaneity invariant
        state_sim_time = (sample_result.state.get("sim_time")
                         if sample_result.state else None)
        image_sim_time = capture_result.sim_time_s

        if state_sim_time is None or image_sim_time is None:
            return CalibratedSample(
                state_result=sample_result,
                capture_result=capture_result,
                state_sim_time_s=state_sim_time,
                image_sim_time_s=image_sim_time,
                sim_time_skew_s=None,
                calibrated=False,
                calibration_method="pause_capture_resume_v1",
                calibration_error=(
                    "missing sim_time on state or image — cannot verify "
                    "simultaneity"
                ),
            )

        skew = abs(image_sim_time - state_sim_time)
        if skew <= sim_time_skew_tolerance_s:
            return CalibratedSample(
                state_result=sample_result, capture_result=capture_result,
                state_sim_time_s=state_sim_time, image_sim_time_s=image_sim_time,
                sim_time_skew_s=skew, calibrated=True,
                calibration_method="pause_capture_resume_v1",
            )
        return CalibratedSample(
            state_result=sample_result, capture_result=capture_result,
            state_sim_time_s=state_sim_time, image_sim_time_s=image_sim_time,
            sim_time_skew_s=skew, calibrated=False,
            calibration_method="pause_capture_resume_v1",
            calibration_error=(
                f"sim_time skew {skew*1000:.1f}ms exceeds tolerance "
                f"{sim_time_skew_tolerance_s*1000:.1f}ms — state and image "
                "describe different physical instants"
            ),
        )

    def capture_screenshot(self, host_capture_dir: Path) -> CaptureResult:
        """Trigger Gazebo's /gui/screenshot service inside the container.

        The service returns `data: true` if it was *invoked*, NOT if it
        actually wrote a file. Yesterday we hit a wedged-renderer state
        where the service kept saying "OK" while no new PNG appeared
        and `ls -t | head -1` returned a 46-min-old image, which an
        agent then consumed as fresh.

        Freshness validation (task #15 from 2026-05-11 session):
          1. List existing PNGs in the dir BEFORE calling the service.
          2. Call /gui/screenshot.
          3. List PNGs AFTER. The new file is the set difference.
          4. If no new file: return error — service lied.
          5. If new file exists: return its path, guaranteed fresh.

        Returns CaptureResult.captured_at (ISO-8601 UTC) so the caller
        can compare against state_sample timestamps for cross-modal
        time alignment.

        Service signature (Harmonic):
          /gui/screenshot   request: gz.msgs.StringMsg (directory)
                            reply:   gz.msgs.Boolean
        """
        import datetime as _dt
        host_capture_dir.mkdir(parents=True, exist_ok=True)
        container_dir = "/workspace/.omnilab/captures"

        # Step 1: snapshot existing PNGs BEFORE invocation.
        pre_cmd = [
            "podman", "exec", self.container_name, "bash", "-c",
            f"mkdir -p {container_dir} && "
            f"ls -1 {container_dir}/*.png 2>/dev/null || true",
        ]
        try:
            pre = self._runner(
                pre_cmd, capture_output=True, text=True, check=False, timeout=5.0,
            )
        except subprocess.TimeoutExpired as e:
            return CaptureResult(
                frame_path=None,
                captured_at=_dt.datetime.now(_dt.UTC).isoformat(),
                error=f"pre-snapshot listing timed out: {e}",
            )
        pre_files: set[str] = {
            ln.strip() for ln in (pre.stdout or "").splitlines() if ln.strip()
        }

        # Step 2: actually call the screenshot service.
        captured_at = _dt.datetime.now(_dt.UTC).isoformat()
        svc_cmd = [
            "podman", "exec", self.container_name, "bash", "-c",
            f"gz service -s /gui/screenshot "
            f"--reqtype gz.msgs.StringMsg --reptype gz.msgs.Boolean "
            f"--timeout 4000 --req 'data: \"{container_dir}\"'",
        ]
        try:
            r = self._runner(
                svc_cmd, capture_output=True, text=True, check=False, timeout=10.0,
            )
        except subprocess.TimeoutExpired as e:
            return CaptureResult(
                frame_path=None, captured_at=captured_at,
                error=f"screenshot timeout: {e}",
            )
        if r.returncode != 0:
            return CaptureResult(
                frame_path=None, captured_at=captured_at,
                error=f"screenshot rc={r.returncode}: {(r.stderr or '')[:200]}",
            )

        # Step 3: re-list, find what's NEW.
        try:
            post = self._runner(
                pre_cmd, capture_output=True, text=True, check=False, timeout=5.0,
            )
        except subprocess.TimeoutExpired as e:
            return CaptureResult(
                frame_path=None, captured_at=captured_at,
                error=f"post-snapshot listing timed out: {e}",
            )
        post_files: set[str] = {
            ln.strip() for ln in (post.stdout or "").splitlines() if ln.strip()
        }
        new_files = post_files - pre_files

        # Step 4: validate. If no NEW file appeared, the service lied
        # (e.g. wedged-renderer state). Return loud error — never
        # silently hand back a stale path.
        if not new_files:
            return CaptureResult(
                frame_path=None, captured_at=captured_at,
                error=(
                    "screenshot service returned ok but no fresh PNG was written "
                    "(GUI renderer likely wedged). Service response: "
                    f"{(r.stdout or '').strip()[:120]}"
                ),
            )

        # Step 5: take the newest NEW file (in case multiple landed,
        # though normally only one).
        container_path = sorted(new_files)[-1]
        return CaptureResult(
            frame_path=str(host_capture_dir / Path(container_path).name),
            captured_at=captured_at,
        )

    def sample(self) -> SampleResult:
        # Run the snapshot script inside the container via stdin. Source
        # ROS first so rclpy is importable.
        import datetime as _dt
        started = _dt.datetime.now(_dt.UTC).isoformat()
        cmd = [
            "podman", "exec", "-i", self.container_name,
            "bash", "-c",
            "source /opt/ros/jazzy/setup.bash && python3 - "
            f"{self.collect_window_seconds}",
        ]
        try:
            r = self._runner(
                cmd,
                input=_SNAPSHOT_SCRIPT,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.collect_window_seconds + self.podman_timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            return SampleResult(
                state={},
                topics_seen=[],
                topics_missing=list(DEFAULT_TOPIC_SET),
                collect_window_seconds=self.collect_window_seconds,
                state_sample_started_at=started,
                state_sample_ended_at=_dt.datetime.now(_dt.UTC).isoformat(),
                raw_error=f"podman exec timed out: {e}",
            )
        ended = _dt.datetime.now(_dt.UTC).isoformat()

        if r.returncode != 0:
            return SampleResult(
                state={},
                topics_seen=[],
                topics_missing=list(DEFAULT_TOPIC_SET),
                collect_window_seconds=self.collect_window_seconds,
                state_sample_started_at=started,
                state_sample_ended_at=ended,
                raw_error=f"podman exec rc={r.returncode}: {r.stderr[:500]}",
            )

        # The script may print warnings before the JSON line. Grab the
        # last non-empty stdout line and try to parse it.
        last_line = ""
        for line in (r.stdout or "").splitlines():
            if line.strip():
                last_line = line
        if not last_line:
            return SampleResult(
                state={}, topics_seen=[],
                topics_missing=list(DEFAULT_TOPIC_SET),
                collect_window_seconds=self.collect_window_seconds,
                state_sample_started_at=started,
                state_sample_ended_at=ended,
                raw_error="snapshot script produced no output",
            )
        try:
            raw = json.loads(last_line)
        except json.JSONDecodeError as e:
            return SampleResult(
                state={}, topics_seen=[],
                topics_missing=list(DEFAULT_TOPIC_SET),
                collect_window_seconds=self.collect_window_seconds,
                state_sample_started_at=started,
                state_sample_ended_at=ended,
                raw_error=f"json decode failed: {e}; stdout={last_line[:200]}",
            )

        if isinstance(raw, dict) and "error" in raw:
            return SampleResult(
                state={}, topics_seen=[],
                topics_missing=list(DEFAULT_TOPIC_SET),
                collect_window_seconds=self.collect_window_seconds,
                state_sample_started_at=started,
                state_sample_ended_at=ended,
                raw_error=f"snapshot error: {raw['error']}",
            )

        result = _to_predicate_state(raw, self.collect_window_seconds)
        result.state_sample_started_at = started
        result.state_sample_ended_at = ended
        return result


# ---- mapping: raw rclpy snapshot → predicate-engine state ----------------


def _quat_to_rpy_deg(q: list[float]) -> list[float]:
    """Convert [x, y, z, w] quaternion → [roll, pitch, yaw] in degrees."""
    x, y, z, w = q
    # Roll (x-axis rotation)
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    # Pitch (y-axis rotation)
    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)
    # Yaw (z-axis rotation)
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    return [math.degrees(roll), math.degrees(pitch), math.degrees(yaw)]


def _is_upright(rpy_deg: list[float], threshold_deg: float = 30.0) -> bool:
    return abs(rpy_deg[0]) < threshold_deg and abs(rpy_deg[1]) < threshold_deg


def _to_predicate_state(raw: dict[str, Any], window_s: float) -> SampleResult:
    """Map the rclpy snapshot dict to the contract-shaped state dict."""
    received = list(raw.get("received") or [])
    wall_now = float(raw.get("wall_now") or 0.0)

    state: dict[str, Any] = {
        "pose": {
            "position": None,
            "orientation_quat": None,
            "orientation_rpy_deg": None,
            "is_upright": None,
        },
        "velocity": {
            "linear": None,
            "angular": None,
        },
        "joints": {},
        "contacts": [],
        "num_feet_in_contact": 0,
        "tf_frames": {},
        # Convenience keys often referenced by predicates written against
        # the legacy example_quadruped_state shape:
        "linear_velocity": {"x": None, "y": None, "z": None},
        "angular_velocity": {"x": None, "y": None, "z": None},
        "orientation": {"roll": None, "pitch": None, "yaw": None},
        "sim_time": raw.get("clock"),
    }

    # /odom is preferred for pose+velocity. Fall back to /model/.../pose
    # for position (no velocity) when /odom is absent.
    odom = raw.get("odom")
    if odom:
        state["pose"]["position"] = odom["pose"]["position"]
        state["pose"]["orientation_quat"] = odom["pose"]["orientation_quat"]
        rpy = _quat_to_rpy_deg(odom["pose"]["orientation_quat"])
        state["pose"]["orientation_rpy_deg"] = rpy
        state["pose"]["is_upright"] = _is_upright(rpy)
        state["velocity"]["linear"] = odom["twist_linear"]
        state["velocity"]["angular"] = odom["twist_angular"]
        # Mirror to legacy keys
        state["linear_velocity"] = {
            "x": odom["twist_linear"][0], "y": odom["twist_linear"][1], "z": odom["twist_linear"][2],
        }
        state["angular_velocity"] = {
            "x": odom["twist_angular"][0], "y": odom["twist_angular"][1], "z": odom["twist_angular"][2],
        }
        state["orientation"] = {"roll": rpy[0], "pitch": rpy[1], "yaw": rpy[2]}
    else:
        pa = raw.get("pose_array")
        if pa and pa.get("poses"):
            base = pa["poses"][0]
            state["pose"]["position"] = base["position"]
            state["pose"]["orientation_quat"] = base["orientation_quat"]
            rpy = _quat_to_rpy_deg(base["orientation_quat"])
            state["pose"]["orientation_rpy_deg"] = rpy
            state["pose"]["is_upright"] = _is_upright(rpy)
            state["orientation"] = {"roll": rpy[0], "pitch": rpy[1], "yaw": rpy[2]}

    # If /odom missing but /imu present, take orientation from IMU.
    imu = raw.get("imu")
    if imu and state["pose"]["orientation_quat"] is None:
        q = imu["orientation_quat"]
        rpy = _quat_to_rpy_deg(q)
        state["pose"]["orientation_quat"] = q
        state["pose"]["orientation_rpy_deg"] = rpy
        state["pose"]["is_upright"] = _is_upright(rpy)
        state["orientation"] = {"roll": rpy[0], "pitch": rpy[1], "yaw": rpy[2]}
    if imu and state["velocity"]["angular"] is None:
        state["velocity"]["angular"] = imu["angular_velocity"]
        state["angular_velocity"] = {
            "x": imu["angular_velocity"][0], "y": imu["angular_velocity"][1], "z": imu["angular_velocity"][2],
        }

    # /joint_states → joints map.
    js = raw.get("joint_state")
    if js:
        for i, n in enumerate(js["name"]):
            state["joints"][n] = {
                "position": js["position"][i] if i < len(js["position"]) else None,
                "velocity": js["velocity"][i] if i < len(js["velocity"]) else None,
                "effort":   js["effort"][i] if i < len(js["effort"]) else None,
            }

    # /tf + /tf_static → frame staleness map.
    for t in raw.get("tf_static") or []:
        state["tf_frames"][f'{t["parent"]} -> {t["child"]}'] = "valid"  # static = never stale
    for t in raw.get("tf") or []:
        age = max(0.0, wall_now - float(t.get("stamp") or wall_now))
        key = f'{t["parent"]} -> {t["child"]}'
        state["tf_frames"][key] = "valid" if age < 1.0 else f"stale_{age:.1f}s"

    topics_seen = list(received)
    topics_missing = [t for t in DEFAULT_TOPIC_SET if t not in received]

    return SampleResult(
        state=state,
        topics_seen=topics_seen,
        topics_missing=topics_missing,
        collect_window_seconds=window_s,
    )
