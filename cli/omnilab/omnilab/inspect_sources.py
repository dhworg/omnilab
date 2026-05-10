"""Concrete Sources impls for `omnilab inspect`. Split from the data
model so tests can mock without touching podman."""

from __future__ import annotations

import re

from .inspect import (
    GazeboState,
    NodeInfo,
    ServiceInfo,
    Sources,
    TFFrame,
    TopicInfo,
    parse_node_line,
    parse_service_list_line,
    parse_topic_bw,
    parse_topic_hz,
    parse_topic_list_line,
)
from .podman import run

# Sourcing ROS inside the container; the project image's /etc/bash.bashrc
# already does this for interactive shells but `podman exec` runs
# non-interactively, so we add the source line explicitly.
_ROS_SOURCE = "source /opt/ros/jazzy/setup.bash"


class PodmanExecSources:
    """Default impl: shell out to `ros2` / `gz` inside the running container.

    Uses the existing `omnilab.podman.run` wrapper so subprocess behavior
    is consistent with the rest of the CLI.
    """

    def __init__(self, container: str, *, hz_window: int = 30) -> None:
        self.container = container
        self.hz_window = hz_window

    def _exec(self, cmd: str) -> tuple[int, str, str]:
        """Run a bash command inside the container with ROS sourced."""
        result = run(
            [
                "podman",
                "exec",
                self.container,
                "bash",
                "-lc",
                f"{_ROS_SOURCE} && {cmd}",
            ]
        )
        return result.returncode, result.stdout, result.stderr

    # ---- nodes ----------------------------------------------------------

    def list_nodes(self) -> list[NodeInfo]:
        rc, out, _ = self._exec("ros2 node list")
        if rc != 0:
            return []
        nodes = []
        for line in out.splitlines():
            n = parse_node_line(line)
            if n is not None:
                nodes.append(n)
        return nodes

    # ---- topics ---------------------------------------------------------

    def list_topics(self) -> list[TopicInfo]:
        rc, out, _ = self._exec("ros2 topic list -t")
        if rc != 0:
            return []
        topics = []
        for line in out.splitlines():
            t = parse_topic_list_line(line)
            if t is not None:
                # Best-effort rate sample — short window so we don't block
                # the whole snapshot.
                rc_hz, hz_out, _ = self._exec(
                    f"timeout 1.5s ros2 topic hz --window {self.hz_window} {t.name}"
                )
                if rc_hz in (0, 124):  # 124 = timeout's normal exit
                    t.rate_hz = parse_topic_hz(hz_out)
                rc_bw, bw_out, _ = self._exec(
                    f"timeout 1.5s ros2 topic bw {t.name}"
                )
                if rc_bw in (0, 124):
                    t.bandwidth_bytes_per_sec = parse_topic_bw(bw_out)
                topics.append(t)
        return topics

    # ---- services -------------------------------------------------------

    def list_services(self) -> list[ServiceInfo]:
        rc, out, _ = self._exec("ros2 service list -t")
        if rc != 0:
            return []
        return [
            s
            for s in (parse_service_list_line(line) for line in out.splitlines())
            if s is not None
        ]

    # ---- tf -------------------------------------------------------------

    def get_tf_tree(self) -> list[TFFrame]:
        # `ros2 run tf2_tools view_frames` writes a PDF; for in-band
        # querying we iterate `tf2_echo` over `ros2 topic info /tf` parents.
        # v0 only reports frames seen on /tf without staleness analysis;
        # Phase B.future adds rclpy-driven staleness via tf2_ros.Buffer.
        rc, out, _ = self._exec("timeout 2s ros2 topic echo --once /tf 2>&1 || true")
        frames: dict[str, TFFrame] = {}
        if rc != 0:
            return []
        # Parse YAML-like ros2 topic echo output for child_frame_id +
        # frame_id pairs. Cheap regex; full parsing is Phase B.future.
        for m in re.finditer(
            r"frame_id:\s*['\"]?(\S+?)['\"]?\s+.*?child_frame_id:\s*['\"]?(\S+?)['\"]?",
            out,
            re.DOTALL,
        ):
            parent, child = m.group(1), m.group(2)
            frames[child] = TFFrame(name=child, parent=parent)
        return list(frames.values())

    # ---- gazebo ---------------------------------------------------------

    def get_gazebo_state(self) -> GazeboState:
        rc, out, _ = self._exec("timeout 1s gz topic --list 2>&1 || true")
        connected = rc == 0 and "/world/" in out
        sim_time: float | None = None
        rtf: float | None = None
        if connected:
            rc_s, stats_out, _ = self._exec(
                "timeout 1.5s gz topic -e -t /stats -n 1 2>&1 || true"
            )
            if rc_s in (0, 124):
                # /stats publishes WorldStatistics msgs with sim_time + rtf.
                m = re.search(r"sim_time\s*\{\s*sec:\s*(\d+).*?nsec:\s*(\d+)", stats_out, re.DOTALL)
                if m:
                    sim_time = int(m.group(1)) + int(m.group(2)) / 1e9
                m_rtf = re.search(r"real_time_factor:\s*([\d.]+)", stats_out)
                if m_rtf:
                    try:
                        rtf = float(m_rtf.group(1))
                    except ValueError:
                        rtf = None
        return GazeboState(connected=connected, sim_time=sim_time, rtf=rtf)


class FakeSources:
    """Static fake for tests / dry-runs / demos."""

    def __init__(
        self,
        nodes: list[NodeInfo] | None = None,
        topics: list[TopicInfo] | None = None,
        services: list[ServiceInfo] | None = None,
        tf_frames: list[TFFrame] | None = None,
        gazebo: GazeboState | None = None,
    ) -> None:
        self._nodes = nodes or []
        self._topics = topics or []
        self._services = services or []
        self._tf = tf_frames or []
        self._gz = gazebo or GazeboState(connected=False)

    def list_nodes(self) -> list[NodeInfo]:
        return list(self._nodes)

    def list_topics(self) -> list[TopicInfo]:
        return list(self._topics)

    def list_services(self) -> list[ServiceInfo]:
        return list(self._services)

    def get_tf_tree(self) -> list[TFFrame]:
        return list(self._tf)

    def get_gazebo_state(self) -> GazeboState:
        return self._gz


__all__ = ["PodmanExecSources", "FakeSources", "Sources"]
