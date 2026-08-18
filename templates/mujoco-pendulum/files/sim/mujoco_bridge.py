#!/usr/bin/env python3
"""MuJoCo → ROS 2 bridge. Steps the model in real time and publishes
/clock (rosgraph_msgs/Clock) and /joint_states (sensor_msgs/JointState),
which is the contract the rest of OmniLab consumes:

  - `omnilab observe` reads /joint_states into `joints.<name>.*` for
    observers.yaml predicates,
  - `omnilab inspect` reads /clock for sim liveness + sim_time,
  - `omnilab record` bags both.

Launched by `omnilab sim` as:
    python3 sim/mujoco_bridge.py --model sim/pendulum.xml [--headless]

With a display (omnilab up passes Wayland/X through), a passive MuJoCo
viewer window opens alongside; --headless skips it. Physics runs either
way — the viewer is a window onto the sim, not the sim itself.
"""

from __future__ import annotations

import argparse
import time

import mujoco
import rclpy
from rclpy.node import Node
from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import JointState

PUBLISH_HZ = 50.0


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


def run(model_path: str, *, headless: bool) -> int:
    model = mujoco.MjModel.from_xml_path(model_path)
    data = mujoco.MjData(model)
    if model.nkey > 0:  # start from the first keyframe when one exists
        mujoco.mj_resetDataKeyframe(model, data, 0)

    rclpy.init()
    node = MujocoBridge(model, data)
    node.get_logger().info(
        f"bridging {model_path}: joints={node.joint_names}, publish={PUBLISH_HZ}Hz"
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
    next_publish = 0.0
    try:
        while rclpy.ok():
            # Real-time pacing: step physics until sim time catches wall time.
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
