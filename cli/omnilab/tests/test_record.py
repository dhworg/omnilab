"""Tests for omnilab.record — argument planners, metadata, manager."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from omnilab.record import (
    CAMERA_EXCLUDE_PATTERNS,
    DEFAULT_EXCLUDE_PATTERNS,
    SCHEMA_VERSION,
    RecordingManager,
    RecordingMetadata,
    auto_recording_id,
    build_record_args,
    build_replay_args,
    env_mismatch_warnings,
    hash_file,
)

# ---- auto-name ----------------------------------------------------------


def test_auto_recording_id_format():
    when = dt.datetime(2026, 5, 10, 12, 34, 56, tzinfo=dt.UTC)
    rid = auto_recording_id("my-project", now=when)
    assert rid == "2026-05-10T12-34-56_my-project"


def test_auto_recording_id_sanitizes_project_name():
    when = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    rid = auto_recording_id("foo bar/baz!", now=when)
    assert " " not in rid
    assert "/" not in rid
    assert "!" not in rid


# ---- hash_file ----------------------------------------------------------


def test_hash_file_missing_returns_none(tmp_path: Path):
    assert hash_file(tmp_path / "nope") is None


def test_hash_file_consistent(tmp_path: Path):
    p = tmp_path / "f.txt"
    p.write_text("hello world")
    h1 = hash_file(p)
    h2 = hash_file(p)
    assert h1 == h2
    assert h1 is not None
    assert h1.startswith("sha256:")


# ---- record args planner ------------------------------------------------


def test_build_record_args_with_exclusions(tmp_path: Path):
    args = build_record_args(
        bag_dir=tmp_path / "bag",
        excluded_patterns=["/tf_static", "/rosout"],
    )
    assert args[:4] == ["ros2", "bag", "record", "--storage"]
    assert "mcap" in args
    assert "--all" in args
    # Each excluded pattern gets its own --exclude.
    excludes = [args[i + 1] for i, a in enumerate(args) if a == "--exclude"]
    assert "/tf_static" in excludes
    assert "/rosout" in excludes


def test_build_record_args_with_whitelist(tmp_path: Path):
    args = build_record_args(
        bag_dir=tmp_path / "bag",
        topics_whitelist=["/cmd_vel", "/odom"],
        excluded_patterns=["should-be-ignored"],
    )
    # Whitelist disables --all + --exclude.
    assert "--all" not in args
    assert "--exclude" not in args
    assert "/cmd_vel" in args
    assert "/odom" in args


def test_build_record_args_compression_default_zstd(tmp_path: Path):
    args = build_record_args(bag_dir=tmp_path / "bag")
    assert "--compression-format" in args
    assert "zstd" in args


def test_build_record_args_max_size_in_bytes(tmp_path: Path):
    args = build_record_args(bag_dir=tmp_path / "bag", max_bag_size_mib=512)
    idx = args.index("--max-bag-size")
    assert int(args[idx + 1]) == 512 * 1024 * 1024


# ---- replay args planner ------------------------------------------------


def test_build_replay_args_basic(tmp_path: Path):
    args = build_replay_args(bag_dir=tmp_path / "bag")
    assert args[:3] == ["ros2", "bag", "play"]
    assert str(tmp_path / "bag") in args


def test_build_replay_args_with_options(tmp_path: Path):
    args = build_replay_args(
        bag_dir=tmp_path / "bag", rate=0.5, start_offset=12.0, loop=True
    )
    assert "--rate" in args
    assert "0.5" in args
    assert "--start-offset" in args
    assert "12.0" in args
    assert "--loop" in args


# ---- env mismatch -------------------------------------------------------


def test_env_mismatch_warning_when_image_differs():
    meta = RecordingMetadata(
        schema_version=SCHEMA_VERSION,
        recording_id="x",
        project="p",
        created_at="2026-05-10T00:00:00+00:00",
        image="ghcr.io/dhworg/foo:v1",
    )
    warnings = env_mismatch_warnings(meta, current_image="ghcr.io/dhworg/foo:v2")
    assert len(warnings) == 1
    assert "image mismatch" in warnings[0]


def test_env_mismatch_no_warning_when_match():
    meta = RecordingMetadata(
        schema_version=SCHEMA_VERSION,
        recording_id="x",
        project="p",
        created_at="2026-05-10T00:00:00+00:00",
        image="ghcr.io/dhworg/foo:v1",
    )
    assert env_mismatch_warnings(meta, current_image="ghcr.io/dhworg/foo:v1") == []


# ---- manager: filesystem layout ----------------------------------------


def _meta(rid: str) -> RecordingMetadata:
    return RecordingMetadata(
        schema_version=SCHEMA_VERSION,
        recording_id=rid,
        project="p",
        created_at="2026-05-10T00:00:00+00:00",
        image="ghcr.io/dhworg/foo:latest",
    )


def test_init_recording_creates_layout(tmp_path: Path):
    mgr = RecordingManager(tmp_path)
    rec_dir = mgr.init_recording(recording_id="r1", metadata=_meta("r1"))
    assert rec_dir == tmp_path / ".omnilab" / "recordings" / "r1"
    assert (rec_dir / "metadata.yaml").exists()


def test_init_recording_refuses_to_overwrite(tmp_path: Path):
    mgr = RecordingManager(tmp_path)
    mgr.init_recording(recording_id="r1", metadata=_meta("r1"))
    with pytest.raises(FileExistsError):
        mgr.init_recording(recording_id="r1", metadata=_meta("r1"))


def test_load_metadata_round_trips(tmp_path: Path):
    mgr = RecordingManager(tmp_path)
    mgr.init_recording(recording_id="r1", metadata=_meta("r1"))
    loaded = mgr.load_metadata("r1")
    assert loaded.recording_id == "r1"
    assert loaded.image == "ghcr.io/dhworg/foo:latest"


def test_update_metadata(tmp_path: Path):
    mgr = RecordingManager(tmp_path)
    mgr.init_recording(recording_id="r1", metadata=_meta("r1"))
    updated = mgr.update_metadata("r1", duration_seconds=12.5)
    assert updated.duration_seconds == 12.5
    # Verify persistence.
    assert mgr.load_metadata("r1").duration_seconds == 12.5


def test_list_recordings(tmp_path: Path):
    mgr = RecordingManager(tmp_path)
    mgr.init_recording(recording_id="b", metadata=_meta("b"))
    mgr.init_recording(recording_id="a", metadata=_meta("a"))
    items = mgr.list_recordings()
    assert [m.recording_id for m in items] == ["a", "b"]  # sorted


def test_list_recordings_empty(tmp_path: Path):
    assert RecordingManager(tmp_path).list_recordings() == []


# ---- defaults reflect spec ----------------------------------------------


def test_default_exclusions_include_spammy_topics():
    assert "/tf_static" in DEFAULT_EXCLUDE_PATTERNS
    assert "/rosout" in DEFAULT_EXCLUDE_PATTERNS


def test_camera_exclusions_present():
    assert any("image_raw" in p for p in CAMERA_EXCLUDE_PATTERNS)
