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
    gazebo: GazeboConfig = Field(default_factory=GazeboConfig)
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
