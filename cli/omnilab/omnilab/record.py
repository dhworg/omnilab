"""Smart bag recording for `omnilab record`.

The split here:
  * Argument planner + metadata model are pure (testable without ros2).
  * Manager handles the filesystem layout and process lifecycle; tests
    use a fake ProcessSpawner.

Per project-spec-v1.md (rev 3) § "Recording" + "v1 must-do" #14:
  - MCAP default, sqlite3 fallback for older bags.
  - zstd compression.
  - Auto-name (timestamp + project) under `.omnilab/recordings/`.
  - Default exclusions for spammy topics; `--topics` whitelist overrides.
  - `--with-cameras` opt-in for image streams.
  - Metadata sidecar so replay can warn on environment mismatch.
  - Background mode for agent workflows: `--start --background`
    returns an id; `--stop <id>` halts.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "1"

# Conservative default exclusion patterns. "spammy or large; rarely
# useful in a debug bag". Can be overridden by --topics whitelist.
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "/tf_static",
    "/rosout",
    "/parameter_events",
    "/dynamic_joint_states",
)
# Camera streams are excluded by default; --with-cameras flips this.
CAMERA_EXCLUDE_PATTERNS: tuple[str, ...] = (
    r"/.*image_raw$",
    r"/.*compressed.*",
    r"/.*camera_info$",
    r"/.*depth.*",
)


# ---- metadata sidecar ----------------------------------------------------


@dataclass
class RecordingMetadata:
    schema_version: str
    recording_id: str
    project: str
    created_at: str
    image: str
    manifest_digest: str | None = None
    observers_hash: str | None = None
    topics_excluded: list[str] = field(default_factory=list)
    topics_whitelist: list[str] | None = None
    duration_seconds: float | None = None
    bag_format: str = "mcap"
    compression: str = "zstd"
    bag_path: str = "bag"
    screencast_path: str | None = None
    pid: int | None = None  # only present while a background recording is active

    def to_yaml(self) -> str:
        return yaml.safe_dump(asdict(self), sort_keys=False, default_flow_style=False)

    @classmethod
    def from_yaml(cls, text: str) -> RecordingMetadata:
        data = yaml.safe_load(text) or {}
        return cls(**data)


# ---- pure helpers --------------------------------------------------------


def auto_recording_id(project: str, *, now: dt.datetime | None = None) -> str:
    """Construct a stable, sortable id for a new recording."""
    ts = (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m-%dT%H-%M-%S")
    safe_project = re.sub(r"[^a-zA-Z0-9_-]", "-", project)
    return f"{ts}_{safe_project}"


def hash_file(path: Path) -> str | None:
    """SHA-256 hash of file contents, or None if missing."""
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def build_record_args(
    *,
    bag_dir: Path,
    topics_whitelist: list[str] | None = None,
    excluded_patterns: list[str] | None = None,
    bag_format: str = "mcap",
    compression: str = "zstd",
    max_bag_size_mib: int = 1024,
) -> list[str]:
    """Pure: build the `ros2 bag record` argv. Caller handles wrapping
    in `bash -lc "source ros && <argv>"`.
    """
    args = ["ros2", "bag", "record", "--storage", bag_format]
    if compression == "zstd":
        args += ["--compression-mode", "file", "--compression-format", "zstd"]
    if max_bag_size_mib > 0:
        args += ["--max-bag-size", str(max_bag_size_mib * 1024 * 1024)]
    args += ["--output", str(bag_dir)]
    if topics_whitelist:
        args += list(topics_whitelist)
    else:
        # Record all + exclude.
        args += ["--all"]
        for pat in excluded_patterns or []:
            args += ["--exclude", pat]
    return args


def build_replay_args(
    *,
    bag_dir: Path,
    rate: float | None = None,
    start_offset: float | None = None,
    loop: bool = False,
) -> list[str]:
    """Pure: build the `ros2 bag play` argv."""
    args = ["ros2", "bag", "play", str(bag_dir)]
    if rate is not None:
        args += ["--rate", str(rate)]
    if start_offset is not None:
        args += ["--start-offset", str(start_offset)]
    if loop:
        args += ["--loop"]
    return args


def detect_screencast_tool() -> str | None:
    """Returns 'wf-recorder' if available, else None.

    Wayland-native; obs-studio etc. would need different invocation and
    aren't bundled. v0 is wf-recorder or warn.
    """
    return shutil.which("wf-recorder")


def env_mismatch_warnings(
    metadata: RecordingMetadata, *, current_image: str | None = None
) -> list[str]:
    """Pure: compute env-mismatch warnings for replay.

    Compares the recording's metadata against the *current* env. Returns
    a list of human-readable warnings. Empty list means no mismatches.
    """
    warnings: list[str] = []
    if current_image and metadata.image != current_image:
        warnings.append(
            f"image mismatch: recorded with {metadata.image!r}, "
            f"replaying under {current_image!r}"
        )
    return warnings


# ---- manager (filesystem + lifecycle) -----------------------------------


class RecordingManager:
    """Manages `.omnilab/recordings/<id>/` layout and metadata.

    Process lifecycle (background mode) is delegated to a spawner so
    tests can substitute a fake.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.recordings_dir = project_dir / ".omnilab" / "recordings"

    def init_recording(
        self,
        *,
        recording_id: str,
        metadata: RecordingMetadata,
    ) -> Path:
        """Create the recording's directory layout and write metadata."""
        rec_dir = self.recordings_dir / recording_id
        rec_dir.mkdir(parents=True, exist_ok=False)
        (rec_dir / "metadata.yaml").write_text(metadata.to_yaml())
        return rec_dir

    def update_metadata(
        self, recording_id: str, **changes: Any
    ) -> RecordingMetadata:
        meta = self.load_metadata(recording_id)
        for k, v in changes.items():
            setattr(meta, k, v)
        (self.recordings_dir / recording_id / "metadata.yaml").write_text(meta.to_yaml())
        return meta

    def load_metadata(self, recording_id: str) -> RecordingMetadata:
        path = self.recordings_dir / recording_id / "metadata.yaml"
        if not path.exists():
            raise FileNotFoundError(f"no recording at {path}")
        return RecordingMetadata.from_yaml(path.read_text())

    def list_recordings(self) -> list[RecordingMetadata]:
        if not self.recordings_dir.exists():
            return []
        out: list[RecordingMetadata] = []
        for entry in sorted(self.recordings_dir.iterdir()):
            meta_path = entry / "metadata.yaml"
            if meta_path.exists():
                out.append(RecordingMetadata.from_yaml(meta_path.read_text()))
        return out

    def stop_background(self, recording_id: str) -> int:
        """SIGTERM the recording's pid, wait briefly, then SIGKILL if needed.

        Returns final exit code (0 on success). Updates metadata duration
        and clears the pid field.
        """
        meta = self.load_metadata(recording_id)
        if meta.pid is None:
            return 0  # already stopped
        try:
            os.kill(meta.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.update_metadata(recording_id, pid=None)
            return 0
        # Wait up to 5s for the recorder to flush and exit.
        try:
            for _ in range(50):
                os.kill(meta.pid, 0)  # raises ProcessLookupError when dead
                _wait(0.1)
        except ProcessLookupError:
            pass
        else:
            with contextlib.suppress(ProcessLookupError):
                os.kill(meta.pid, signal.SIGKILL)

        # Compute duration from metadata.created_at to now.
        created = dt.datetime.fromisoformat(meta.created_at)
        duration = (dt.datetime.now(dt.UTC) - created).total_seconds()
        self.update_metadata(recording_id, pid=None, duration_seconds=duration)
        return 0


def _wait(seconds: float) -> None:
    """Wrapper for time.sleep; isolated so tests can monkeypatch."""
    time.sleep(seconds)


# ---- screencast spawner --------------------------------------------------


def spawn_screencast(rec_dir: Path) -> tuple[int | None, str | None]:
    """Spawn wf-recorder if available. Returns (pid, output_path)."""
    binary = detect_screencast_tool()
    if binary is None:
        return None, None
    out_path = rec_dir / "screencast.mp4"
    try:
        proc = subprocess.Popen(  # noqa: S603 — args are controlled
            [binary, "-f", str(out_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid, str(out_path.relative_to(rec_dir.parent))
    except OSError:
        return None, None
