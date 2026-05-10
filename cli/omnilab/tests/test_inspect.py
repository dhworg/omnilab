"""Tests for omnilab.inspect — parsers, snapshot builder, formatters."""

from __future__ import annotations

import json

import pytest

from omnilab.inspect import (
    SCHEMA_VERSION,
    GazeboState,
    NodeInfo,
    ServiceInfo,
    TFFrame,
    TopicInfo,
    build_snapshot,
    parse_node_line,
    parse_ps_for_pid,
    parse_service_list_line,
    parse_topic_bw,
    parse_topic_hz,
    parse_topic_list_line,
)
from omnilab.inspect_sources import FakeSources
from omnilab.inspect_tui import (
    _format_gazebo,
    _format_nodes,
    _format_services,
    _format_tf,
    _format_topics,
)

# ---- node parser --------------------------------------------------------


def test_parse_node_top_level():
    n = parse_node_line("/turtlebot3_diff_drive")
    assert n is not None
    assert n.name == "/turtlebot3_diff_drive"
    assert n.namespace == "/"


def test_parse_node_namespaced():
    n = parse_node_line("/sim/imu")
    assert n is not None
    assert n.name == "/sim/imu"
    assert n.namespace == "/sim"


def test_parse_node_deep_namespace():
    n = parse_node_line("/sim/sensors/lidar")
    assert n is not None
    assert n.namespace == "/sim/sensors"


def test_parse_node_skips_blank_and_comments():
    assert parse_node_line("") is None
    assert parse_node_line("   ") is None
    assert parse_node_line("not_a_node") is None


# ---- topic parser -------------------------------------------------------


def test_parse_topic_list_line():
    t = parse_topic_list_line("/cmd_vel [geometry_msgs/msg/Twist]")
    assert t is not None
    assert t.name == "/cmd_vel"
    assert t.type == "geometry_msgs/msg/Twist"


def test_parse_topic_list_with_leading_whitespace():
    t = parse_topic_list_line("    /tf [tf2_msgs/msg/TFMessage]   ")
    assert t is not None
    assert t.name == "/tf"


def test_parse_topic_list_invalid():
    assert parse_topic_list_line("not a topic") is None
    assert parse_topic_list_line("") is None


# ---- service parser -----------------------------------------------------


def test_parse_service_list_line():
    s = parse_service_list_line("/get_parameters [rcl_interfaces/srv/GetParameters]")
    assert s is not None
    assert s.name == "/get_parameters"
    assert s.type == "rcl_interfaces/srv/GetParameters"


# ---- topic hz / bw parsers ----------------------------------------------


def test_parse_topic_hz_normal():
    out = """\
average rate: 29.987
    min: 0.033s max: 0.034s std dev: 0.00012s window: 30
"""
    assert parse_topic_hz(out) == pytest.approx(29.987, rel=1e-3)


def test_parse_topic_hz_no_match():
    assert parse_topic_hz("WARNING: failed") is None


def test_parse_topic_bw_kb():
    out = "average: 12.5KB/s\n  mean: 1.2KB ..."
    bw = parse_topic_bw(out)
    assert bw == pytest.approx(12.5 * 1024, rel=1e-3)


def test_parse_topic_bw_mb():
    out = "average: 1.0MB/s\n"
    bw = parse_topic_bw(out)
    assert bw == pytest.approx(1024 * 1024, rel=1e-3)


def test_parse_topic_bw_no_match():
    assert parse_topic_bw("nothing") is None


# ---- ps parser ----------------------------------------------------------


def test_parse_ps_basic():
    ps = """\
  PID %CPU   RSS
  101  2.5  10240
  102  0.1   1024
"""
    cpu, mem = parse_ps_for_pid(ps, 101)
    assert cpu == pytest.approx(2.5)
    assert mem == pytest.approx(10240 / 1024.0)  # 10 MB


def test_parse_ps_missing_pid():
    cpu, mem = parse_ps_for_pid("PID %CPU RSS\n  1 0 0", 999)
    assert cpu is None
    assert mem is None


# ---- snapshot builder ---------------------------------------------------


def test_build_snapshot_with_fake_sources():
    fake = FakeSources(
        nodes=[NodeInfo(name="/n1"), NodeInfo(name="/sim/n2", namespace="/sim")],
        topics=[TopicInfo(name="/t1", type="x/Y", rate_hz=10.0)],
        services=[ServiceInfo(name="/s1", type="srv/A")],
        tf_frames=[TFFrame(name="base_link", parent="odom")],
        gazebo=GazeboState(connected=True, sim_time=12.3, rtf=0.97),
    )
    snap = build_snapshot(fake, container="proj-x")
    assert snap.container == "proj-x"
    assert snap.schema_version == SCHEMA_VERSION
    assert len(snap.nodes) == 2
    assert snap.gazebo.rtf == pytest.approx(0.97)


def test_snapshot_to_json_dict_round_trips():
    snap = build_snapshot(FakeSources(), container="empty")
    d = snap.to_json_dict()
    # Verify it serializes to valid JSON.
    serialized = json.dumps(d)
    parsed = json.loads(serialized)
    assert parsed["schema_version"] == SCHEMA_VERSION
    assert parsed["container"] == "empty"
    assert parsed["nodes"] == []
    assert parsed["topics"] == []
    assert parsed["gazebo"]["connected"] is False


def test_snapshot_timestamp_is_iso8601():
    snap = build_snapshot(FakeSources(), container="t")
    # Will raise if not a parseable ISO timestamp.
    import datetime as dt

    parsed = dt.datetime.fromisoformat(snap.timestamp)
    assert parsed.tzinfo is not None  # we always emit UTC


# ---- formatters ---------------------------------------------------------


def test_format_nodes_handles_empty():
    snap = build_snapshot(FakeSources(), container="t")
    assert "no nodes" in _format_nodes(snap)


def test_format_nodes_with_metrics():
    fake = FakeSources(nodes=[NodeInfo(name="/n", cpu_percent=12.5, mem_mb=42.0)])
    snap = build_snapshot(fake, container="t")
    out = _format_nodes(snap)
    assert "/n" in out
    assert "12.5%" in out


def test_format_topics_with_rate():
    fake = FakeSources(
        topics=[TopicInfo(name="/cmd_vel", type="x/Y", rate_hz=30.0, bandwidth_bytes_per_sec=2048)]
    )
    snap = build_snapshot(fake, container="t")
    out = _format_topics(snap)
    assert "/cmd_vel" in out
    assert "30.0Hz" in out


def test_format_tf_marks_stale():
    fake = FakeSources(tf_frames=[TFFrame(name="x", parent="y", stale=True)])
    snap = build_snapshot(fake, container="t")
    out = _format_tf(snap)
    assert "STALE" in out


def test_format_gazebo_disconnected():
    snap = build_snapshot(FakeSources(), container="t")
    assert "no Gazebo" in _format_gazebo(snap)


def test_format_services_empty():
    snap = build_snapshot(FakeSources(), container="t")
    assert "no services" in _format_services(snap)


# ---- snapshot wired through the CLI ------------------------------------


def test_inspect_json_via_cli(monkeypatch, tmp_path):
    """`--json inspect` plumbs through to PodmanExecSources, which we
    intercept via monkeypatch and replace with FakeSources."""
    from typer.testing import CliRunner

    from omnilab import inspect_sources as ins
    from omnilab.cli import app

    # Create a project so manifest validates.
    project = tmp_path / "tproj"
    runner = CliRunner()
    runner.invoke(app, ["new", "tproj", "--directory", str(project)])

    fake = FakeSources(
        nodes=[NodeInfo(name="/n")],
        topics=[TopicInfo(name="/t", type="x/Y")],
        gazebo=GazeboState(connected=True, sim_time=1.0, rtf=1.0),
    )
    monkeypatch.setattr(ins, "PodmanExecSources", lambda *a, **kw: fake)

    # Patch container check + has_podman so we get past the gates.
    monkeypatch.setattr("omnilab.cli.has_podman", lambda: True)
    monkeypatch.setattr("omnilab.cli.container_running", lambda _name: True)

    result = runner.invoke(
        app, ["--json", "inspect", "--directory", str(project)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["container"] == "tproj"
    assert payload["nodes"][0]["name"] == "/n"
    assert payload["gazebo"]["connected"] is True
