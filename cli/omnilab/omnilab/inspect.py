"""Snapshot data model + parsers for `omnilab inspect`.

The split here:
  * Pure functions and dataclasses live in this module — fully testable
    without rclpy, podman, or a running container.
  * Source impls (PodmanExecSources, FakeSources) live in
    `inspect_sources.py`.
  * The textual TUI lives in `inspect_tui.py` (lazy-imported).

JSON schema (stable; renames are breaking):

    {
      "schema_version": "1",
      "timestamp": "2026-05-10T12:34:56.789012+00:00",
      "container": "my-project",
      "nodes": [
        {"name": "/n", "namespace": "/", "cpu_percent": 1.2,
         "mem_mb": 42.0, "warnings": []}
      ],
      "topics": [
        {"name": "/t", "type": "ns/Msg", "rate_hz": 30.0,
         "bandwidth_bytes_per_sec": 1024, "publisher_count": 1,
         "subscriber_count": 0}
      ],
      "services": [{"name": "/s", "type": "ns/Srv"}],
      "tf_frames": [
        {"name": "base_link", "parent": "odom", "stale": false,
         "missing": false}
      ],
      "gazebo": {
        "connected": true, "sim_time": 12.3, "rtf": 0.97
      }
    }
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

SCHEMA_VERSION = "1"


# ---- data model ----------------------------------------------------------


@dataclass
class NodeInfo:
    name: str
    namespace: str = "/"
    cpu_percent: float | None = None
    mem_mb: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class TopicInfo:
    name: str
    type: str
    rate_hz: float | None = None
    bandwidth_bytes_per_sec: float | None = None
    publisher_count: int = 0
    subscriber_count: int = 0


@dataclass
class ServiceInfo:
    name: str
    type: str


@dataclass
class TFFrame:
    name: str
    parent: str | None = None
    stale: bool = False
    missing: bool = False


@dataclass
class GazeboState:
    connected: bool
    sim_time: float | None = None
    rtf: float | None = None


@dataclass
class InspectSnapshot:
    timestamp: str
    container: str
    nodes: list[NodeInfo]
    topics: list[TopicInfo]
    services: list[ServiceInfo]
    tf_frames: list[TFFrame]
    gazebo: GazeboState
    schema_version: str = SCHEMA_VERSION

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "container": self.container,
            "nodes": [asdict(n) for n in self.nodes],
            "topics": [asdict(t) for t in self.topics],
            "services": [asdict(s) for s in self.services],
            "tf_frames": [asdict(f) for f in self.tf_frames],
            "gazebo": asdict(self.gazebo),
        }


# ---- parsers (pure, easy to test) ---------------------------------------


def parse_node_line(line: str) -> NodeInfo | None:
    """Parse one line of `ros2 node list` output.

    >>> parse_node_line('/turtlebot3_diff_drive').namespace
    '/'
    >>> parse_node_line('/sim/imu').namespace
    '/sim'
    """
    name = line.strip()
    if not name or not name.startswith("/"):
        return None
    if "/" in name[1:]:
        prefix = name[: name.rfind("/")]
        ns = prefix or "/"
    else:
        ns = "/"
    return NodeInfo(name=name, namespace=ns)


def parse_topic_list_line(line: str) -> TopicInfo | None:
    """Parse one line of `ros2 topic list -t` output.

    Line shape: `/topic_name [package_name/msg/MessageName]`.
    """
    m = re.match(r"^\s*(\S+)\s+\[(.+)\]\s*$", line)
    if not m:
        return None
    return TopicInfo(name=m.group(1), type=m.group(2))


def parse_service_list_line(line: str) -> ServiceInfo | None:
    """Parse one line of `ros2 service list -t` output."""
    m = re.match(r"^\s*(\S+)\s+\[(.+)\]\s*$", line)
    if not m:
        return None
    return ServiceInfo(name=m.group(1), type=m.group(2))


def parse_topic_hz(out: str) -> float | None:
    """Extract average rate (Hz) from `ros2 topic hz` output.

    Output looks like:
        average rate: 29.987
            min: 0.033s max: 0.034s std dev: 0.00012s window: 30
    """
    m = re.search(r"average rate:\s*([\d.]+)", out)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def parse_topic_bw(out: str) -> float | None:
    """Extract bandwidth (bytes/sec) from `ros2 topic bw` output.

    Sample:
        average: 12.34KB/s
        mean: 1.234KB min: 0.5KB max: 2.0KB window: 100
    """
    m = re.search(r"average:\s*([\d.]+)\s*(B|KB|MB|GB)/s", out, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
    return value * multipliers[unit]


def parse_ps_for_pid(ps_out: str, pid: int) -> tuple[float | None, float | None]:
    """Extract (cpu_percent, mem_mb) for a pid from `ps -e -o pid,%cpu,rss`.

    rss is in kilobytes; we return MB.
    """
    for line in ps_out.splitlines():
        parts = line.split()
        if not parts or parts[0] != str(pid):
            continue
        try:
            cpu = float(parts[1])
            rss_kb = float(parts[2])
            return cpu, rss_kb / 1024.0
        except (ValueError, IndexError):
            return None, None
    return None, None


# ---- snapshot builder ---------------------------------------------------


class Sources(Protocol):
    """The data sources `build_snapshot` consumes. Mock-friendly."""

    def list_nodes(self) -> list[NodeInfo]: ...

    def list_topics(self) -> list[TopicInfo]: ...

    def list_services(self) -> list[ServiceInfo]: ...

    def get_tf_tree(self) -> list[TFFrame]: ...

    def get_gazebo_state(self) -> GazeboState: ...


def build_snapshot(sources: Sources, *, container: str) -> InspectSnapshot:
    """Pure function: assemble a snapshot from any Sources impl."""
    return InspectSnapshot(
        timestamp=dt.datetime.now(dt.UTC).isoformat(),
        container=container,
        nodes=sources.list_nodes(),
        topics=sources.list_topics(),
        services=sources.list_services(),
        tf_frames=sources.get_tf_tree(),
        gazebo=sources.get_gazebo_state(),
    )
