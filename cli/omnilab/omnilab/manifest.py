"""Pydantic schema for `omnilab.yaml` per project-spec-v1.md § Manifest schema."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class RosConfig(BaseModel):
    """ROS 2 runtime config inside the project container."""

    model_config = ConfigDict(extra="forbid")

    rmw: str = "rmw_cyclonedds_cpp"
    domain_id: int = Field(default=42, ge=0, le=232)

    @field_validator("rmw")
    @classmethod
    def _check_rmw(cls, v: str) -> str:
        # Spec pins Cyclone DDS, but other RMWs are allowed for advanced
        # users; warn-via-validation if they use something off-list.
        known = {
            "rmw_cyclonedds_cpp",
            "rmw_fastrtps_cpp",
            "rmw_zenoh_cpp",
        }
        if v not in known:
            # Don't fail — let the user override. Schema remains permissive.
            pass
        return v


class GazeboDefaults(BaseModel):
    """Tuned-for-iGPU defaults per spec § "v1 must-do" #6."""

    model_config = ConfigDict(extra="forbid")

    shadows: bool = False
    camera_fps: int = Field(default=15, ge=1, le=240)
    camera_resolution: tuple[int, int] = (320, 240)


class GazeboConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_world: str | None = None
    defaults: GazeboDefaults = Field(default_factory=GazeboDefaults)


class HardwareConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    micro_ros: Literal["enabled", "disabled"] = "enabled"
    boards: list[str] = Field(default_factory=list)


Simulator = Literal["gazebo", "mujoco"]


class MujocoConfig(BaseModel):
    """MuJoCo project config (simulator: mujoco). Paths are relative to
    the project dir, which is mounted at /workspace in the container."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    # MJCF model file. Required to launch `omnilab sim`.
    model: str | None = None
    # Optional ROS bridge script (steps the sim, publishes /clock +
    # /joint_states). When absent, `omnilab sim` falls back to the plain
    # mujoco viewer, which runs the physics but publishes nothing.
    bridge: str | None = None


class PairConfig(BaseModel):
    """Set by `omnilab pair init/join` and persisted in omnilab.yaml."""

    model_config = ConfigDict(extra="forbid")

    domain_id: int = Field(..., ge=0, le=232)
    config: Literal["simple_discovery", "discovery_server"] = "simple_discovery"


GpuMode = Literal["auto", "igpu", "nvidia"]


class OmnilabManifest(BaseModel):
    """Top-level `omnilab.yaml` schema."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    host_min_version: str = "0.1.0"
    image: str = Field(min_length=1)
    ros: RosConfig = Field(default_factory=RosConfig)
    # Which simulator this project drives. Gazebo remains the primary,
    # full-featured path (Layer 2 frame capture, gz introspection). MuJoCo
    # is supported at the ROS layer: sim launch, observe Layer 1, tune,
    # record, inspect-via-/clock all work; capture degrades honestly to
    # no_image_source. Per spec amendment 2026-08-18.
    simulator: Simulator = "gazebo"
    gazebo: GazeboConfig = Field(default_factory=GazeboConfig)
    mujoco: MujocoConfig = Field(default_factory=MujocoConfig)
    gpu: GpuMode = "auto"
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    # Path to observers.yaml relative to the project dir. Optional —
    # used by `omnilab observe`. Phase B.4 step 8 onwards.
    observers: str | None = None
    # Set by `omnilab pair init/join`; absent when not paired.
    pair: PairConfig | None = None
    skills: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        # Project name maps to a podman container/network name; keep it
        # safe for those. ASCII alnum + dash + underscore. We use an
        # explicit charset rather than str.isalnum() because the latter
        # accepts Unicode letters (e.g. "café") that podman won't honor.
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if not v or any(ch not in allowed for ch in v):
            msg = f"name '{v}' must be ASCII alphanumeric with optional '-' or '_'"
            raise ValueError(msg)
        if v[0] in "-_":
            raise ValueError(f"name '{v}' must start with a letter or digit")
        return v

    @field_validator("image")
    @classmethod
    def _check_image_ref(cls, v: str) -> str:
        # Either tag-form (registry/repo:tag) or digest-form
        # (registry/repo@sha256:...). Per spec, real projects MUST be
        # digest-pinned, but `omnilab new` ships tag-form by default and
        # the user pins later. We accept both here.
        if "@sha256:" not in v and ":" not in v:
            raise ValueError(
                f"image '{v}' must be tag- or digest-form (e.g. 'foo:latest' or 'foo@sha256:...')"
            )
        return v

    @classmethod
    def from_yaml(cls, path: Path | str) -> OmnilabManifest:
        """Load and validate an omnilab.yaml file."""
        path = Path(path)
        with path.open() as f:
            data = yaml.safe_load(f) or {}
        return cls.model_validate(data)

    @property
    def effective_domain_id(self) -> int:
        """The ROS_DOMAIN_ID the container should actually run on.

        A pairing derives its domain from the shared code, so once paired
        it must win over the manifest default — otherwise the two peers
        write a matching Cyclone config and then start their containers on
        different domains, which looks exactly like "pairing did nothing".
        """
        return self.pair.domain_id if self.pair else self.ros.domain_id


def write_pair_config(project_dir: Path, pair: PairConfig) -> Path:
    """Persist the `pair:` block into omnilab.yaml, leaving the rest alone.

    Re-dumps the document rather than editing in place, so YAML comments
    are not preserved — same tradeoff `tune --save` already makes. Key
    order is preserved so diffs stay readable.
    """
    path = Path(project_dir) / "omnilab.yaml"
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    data["pair"] = pair.model_dump()
    with path.open("w") as f:
        yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False)
    return path
