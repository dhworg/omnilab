"""Derived semantic layer for `omnilab observe`.

Per the agent-OS design conversation 2026-05-12:

  Raw state ("base_link is at quaternion (0.7, 0, 0, 0.7)") is opaque
  to an agent that doesn't want to do trigonometry. The derived layer
  precomputes semantic verdicts ("body_orientation_class:
  design_orientation", "is_stationary: true", "joints_at_limit: []")
  server-side, so the agent reads ready-made facts in plain language
  instead of redoing the math on every call.

  Critical contract (feedback_calibration_simultaneity.md):
  Every derived field is computed from the state+image pair captured
  in the SAME `calibrated_sample()` call. It inherits that pair's
  sim_time. There is no "background derivation" or "cached
  classification" — derivations live and die with their calibrated
  sample.

  Motion-rate fields (body_translation_m_per_s, body_yaw_rate_deg_per_s)
  use the history of recent calibrated samples to compute deltas.
  Each rate is tagged with the (sim_time_prev, sim_time_curr) pair it
  was derived from, so the agent can see the time window the rate
  was computed across.

Config:
  Per-project `derived_config.yaml` (sibling of omnilab.yaml).
  Captures URDF-specific facts the layer can't infer:
    - ground_plane_z_m
    - body_link name
    - design_orientation_rpy_deg (what "upright" means for this URDF)
    - tolerances (stationary, joint-limit-ε)
    - joint limits

History:
  `.omnilab/history.jsonl` in the project dir. Append-only, one
  calibrated sample per line, capped at 100 most recent. Read by
  rate-computation fields.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ---- per-robot config ----------------------------------------------------


@dataclass
class DerivedConfig:
    """Per-robot configuration for the derived layer. Loaded from
    derived_config.yaml at observe time. Unknown fields fall back to
    sensible defaults so a project without a config still gets useful
    derived fields, just with weaker semantics.
    """

    ground_plane_z_m: float = 0.0
    body_link: str = "base_link"
    # The orientation that means "the robot is in its design rest pose"
    # for THIS robot. For our quadruped this is roll=π/2 (90°) because
    # the URDF was designed lying-on-side. Other robots: 0,0,0.
    design_orientation_rpy_deg: list[float] = field(
        default_factory=lambda: [0.0, 0.0, 0.0]
    )
    # How far from design orientation before we call it "tipped"
    max_tilt_from_design_deg: float = 30.0
    # Body must be this far above ground to count as "standing"
    min_standing_height_m: float = 0.15
    # Motion thresholds for "is_stationary"
    stationary_translation_tol_m_per_s: float = 0.01
    stationary_rotation_tol_deg_per_s: float = 2.0
    # ε for "joint at its hardware limit"
    joint_limit_tolerance_rad: float = 0.05
    # joint_name -> (min_rad, max_rad). From URDF; user fills this.
    joint_limits: dict[str, list[float]] = field(default_factory=dict)
    # Default camera config for `omnilab observe --capture`. Without
    # this, the gz GUI's default camera is far from origin and the
    # spawned robot shows up as an unreadable dot. Task #22.
    #
    # Specified as a WORLD-frame camera position + a WORLD-frame
    # look-at target. The capture path computes the camera orientation
    # via standard look-at math and calls `gz service /gui/move_to/pose`
    # to position the gz GUI's main camera.
    #
    # We deliberately do NOT use `/gui/follow` + `/gui/follow/offset`
    # because the offset gets re-interpreted in the entity's local frame
    # — for a URDF with a non-identity spawn rotation (like this
    # quadruped at roll=π/2), the offset ends up pointing sideways or
    # downward instead of upward, putting the camera under the floor.
    # World-frame absolute positioning is unambiguous regardless of how
    # the entity is oriented.
    camera_world_position_xyz: list[float] | None = None  # e.g. [2.0, -2.0, 1.5]
    camera_look_at_xyz: list[float] = field(
        default_factory=lambda: [0.0, 0.0, 0.2]  # robot's typical body height
    )

    @classmethod
    def load(cls, project_dir: Path) -> DerivedConfig:
        """Load from project_dir/derived_config.yaml. Returns defaults
        if the file doesn't exist — the layer degrades gracefully.
        """
        cfg_path = project_dir / "derived_config.yaml"
        if not cfg_path.exists():
            return cls()
        data = yaml.safe_load(cfg_path.read_text()) or {}
        return cls(
            ground_plane_z_m=float(data.get("ground_plane_z_m", 0.0)),
            body_link=str(data.get("body_link", "base_link")),
            design_orientation_rpy_deg=list(
                data.get("design_orientation_rpy_deg", [0.0, 0.0, 0.0])
            ),
            max_tilt_from_design_deg=float(
                data.get("max_tilt_from_design_deg", 30.0)
            ),
            min_standing_height_m=float(data.get("min_standing_height_m", 0.15)),
            stationary_translation_tol_m_per_s=float(
                data.get("stationary_translation_tol_m_per_s", 0.01)
            ),
            stationary_rotation_tol_deg_per_s=float(
                data.get("stationary_rotation_tol_deg_per_s", 2.0)
            ),
            joint_limit_tolerance_rad=float(
                data.get("joint_limit_tolerance_rad", 0.05)
            ),
            joint_limits=dict(data.get("joint_limits", {})),
            camera_world_position_xyz=(
                list((data.get("camera_default") or {}).get("world_position_xyz"))
                if (data.get("camera_default") or {}).get("world_position_xyz") is not None
                else None
            ),
            camera_look_at_xyz=list(
                (data.get("camera_default") or {}).get(
                    "look_at_xyz", [0.0, 0.0, 0.2]
                )
            ),
        )


# ---- history persistence ------------------------------------------------


def _history_path(project_dir: Path) -> Path:
    return project_dir / ".omnilab" / "history.jsonl"


def append_history(project_dir: Path, record: dict[str, Any], *, cap: int = 100) -> None:
    """Append one calibrated sample to the per-project history file.
    Rotates the file when it exceeds `cap` lines to keep it small.
    """
    p = _history_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    # Rotate if too long.
    lines = p.read_text().splitlines()
    if len(lines) > cap:
        p.write_text("\n".join(lines[-cap:]) + "\n")


def read_recent_history(project_dir: Path, *, n: int = 5) -> list[dict[str, Any]]:
    """Return the last n history entries (oldest first)."""
    p = _history_path(project_dir)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in p.read_text().splitlines()[-n:]:
        ln = raw.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


# ---- the derivation itself ----------------------------------------------


def _angular_distance_deg(
    a_rpy: list[float], b_rpy: list[float], *, ignore_yaw: bool = True
) -> float:
    """Approximate angular distance between two RPY orientations.

    Treats each axis difference separately, normalizes to [-180, 180],
    sums the absolute values. Good enough for "is this orientation
    close to the design pose" checks; not a proper SO(3) metric.

    By default IGNORES yaw — "upright" is about roll+pitch, not which
    way the robot is facing. A robot facing east vs west is still
    upright. Pass ignore_yaw=False only if heading matters for the
    classification (rare).
    """
    axes = 2 if ignore_yaw else 3  # roll, pitch [, yaw]
    total = 0.0
    for i in range(axes):
        d = a_rpy[i] - b_rpy[i]
        while d > 180:
            d -= 360
        while d < -180:
            d += 360
        total += abs(d)
    return total


def compute_derived(  # noqa: PLR0912, PLR0915
    state: dict[str, Any],
    config: DerivedConfig,
    sim_time_s: float | None,
    *,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute the derived semantic block.

    Returns a dict that's safe to embed in the observe JSON output.
    Every field carries the sim_time it was computed at (inherited
    from the calibrated sample). Motion-rate fields additionally
    carry the (sim_time_prev, sim_time_curr) pair they were derived
    from, so the agent knows the temporal window.
    """
    derived: dict[str, Any] = {
        "computed_at_sim_time_s": sim_time_s,
    }

    pose = state.get("pose") or {}
    pos = pose.get("position") or [None, None, None]
    rpy = pose.get("orientation_rpy_deg") or [None, None, None]

    # --- body height ---
    if pos[2] is not None:
        height = pos[2] - config.ground_plane_z_m
        derived["body_height_above_ground_m"] = round(height, 4)

    # --- orientation class ---
    if all(r is not None for r in rpy):
        tilt = _angular_distance_deg(list(rpy), config.design_orientation_rpy_deg)
        derived["body_tilt_from_design_orientation_deg"] = round(tilt, 2)
        if tilt < config.max_tilt_from_design_deg:
            derived["body_orientation_class"] = "design_orientation"
        elif tilt > 150:
            derived["body_orientation_class"] = "inverted"
        else:
            derived["body_orientation_class"] = "tipped"

    # --- joints at their hardware limit ---
    joints_at_limit: list[str] = []
    joints = state.get("joints") or {}
    for jname, (jmin, jmax) in config.joint_limits.items():
        j = joints.get(jname)
        if not j or j.get("position") is None:
            continue
        p = j["position"]
        if abs(p - jmin) < config.joint_limit_tolerance_rad or abs(p - jmax) < config.joint_limit_tolerance_rad:
            joints_at_limit.append(jname)
    derived["joints_at_limit"] = joints_at_limit
    derived["any_joint_at_limit"] = bool(joints_at_limit)

    # --- motion rates (require history) ---
    derived["motion_rates"] = None  # populated below if possible
    if history and sim_time_s is not None and len(history) >= 1:
        prev = history[-1]
        prev_sim = prev.get("sim_time_s")
        if prev_sim is not None and sim_time_s > prev_sim:
            dt = sim_time_s - prev_sim
            prev_pos = (prev.get("pose") or {}).get("position") or [None] * 3
            prev_rpy = (prev.get("pose") or {}).get("orientation_rpy_deg") or [None] * 3
            if all(p is not None for p in prev_pos) and all(p is not None for p in pos):
                dx = pos[0] - prev_pos[0]
                dy = pos[1] - prev_pos[1]
                dz = pos[2] - prev_pos[2]
                trans_rate = math.sqrt(dx * dx + dy * dy + dz * dz) / dt
                mr: dict[str, Any] = {
                    "sim_time_window_s": [round(prev_sim, 4), round(sim_time_s, 4)],
                    "dt_s": round(dt, 4),
                    "translation_m_per_s": round(trans_rate, 5),
                }
                if all(r is not None for r in prev_rpy) and all(r is not None for r in rpy):
                    dyaw = rpy[2] - prev_rpy[2]
                    while dyaw > 180:
                        dyaw -= 360
                    while dyaw < -180:
                        dyaw += 360
                    yaw_rate = dyaw / dt
                    droll = rpy[0] - prev_rpy[0]
                    while droll > 180:
                        droll -= 360
                    while droll < -180:
                        droll += 360
                    dpitch = rpy[1] - prev_rpy[1]
                    while dpitch > 180:
                        dpitch -= 360
                    while dpitch < -180:
                        dpitch += 360
                    mr["roll_rate_deg_per_s"] = round(droll / dt, 4)
                    mr["pitch_rate_deg_per_s"] = round(dpitch / dt, 4)
                    mr["yaw_rate_deg_per_s"] = round(yaw_rate, 4)
                    mr["is_stationary"] = bool(
                        trans_rate < config.stationary_translation_tol_m_per_s
                        and abs(yaw_rate) < config.stationary_rotation_tol_deg_per_s
                    )
                    mr["stationary_tolerance_used"] = {
                        "translation_m_per_s": config.stationary_translation_tol_m_per_s,
                        "rotation_deg_per_s": config.stationary_rotation_tol_deg_per_s,
                    }
                derived["motion_rates"] = mr

    # --- standing hypothesis ---
    # This is the LLM-readable verdict: combines orientation + height +
    # motion stability. It is a HYPOTHESIS — the cross-modal loop's
    # job is to validate it against the image. The agent must compare
    # this hypothesis with what it sees.
    height_ok = derived.get("body_height_above_ground_m", -math.inf) >= config.min_standing_height_m
    orient_ok = derived.get("body_orientation_class") == "design_orientation"
    motion = derived.get("motion_rates") or {}
    stationary_ok = motion.get("is_stationary", True)  # default True if no history yet
    derived["standing_hypothesis"] = {
        "value": bool(height_ok and orient_ok and stationary_ok),
        "components": {
            "height_ok": bool(height_ok),
            "orientation_ok": bool(orient_ok),
            "stationary_ok": bool(stationary_ok),
        },
        "thresholds_used": {
            "min_height_m": config.min_standing_height_m,
            "max_tilt_from_design_deg": config.max_tilt_from_design_deg,
        },
        # The honest disclaimer the agent reads:
        "note": (
            "This is a derived hypothesis from numeric state. The "
            "agent MUST verify against the image at sim_time "
            f"{sim_time_s} before quoting this as truth. See "
            "feedback_calibration_simultaneity.md."
        ),
    }

    return derived


# ---- history record builder ---------------------------------------------


def build_history_record(
    *,
    sim_time_s: float | None,
    state: dict[str, Any],
    image_path: str | None,
    verification_mode: str,
) -> dict[str, Any]:
    """Build a minimal history record to persist after a calibrated
    observe call. Keeps only pose + sim_time + image_path so the file
    stays small.
    """
    pose = state.get("pose") or {}
    return {
        "sim_time_s": sim_time_s,
        "pose": {
            "position": pose.get("position"),
            "orientation_rpy_deg": pose.get("orientation_rpy_deg"),
        },
        "image_path": image_path,
        "verification_mode": verification_mode,
    }
