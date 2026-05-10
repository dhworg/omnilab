"""Phase B.6 'agent loop' smoke test (callable in CI without ROS).

Per project-spec-v1.md (rev 3) § "Smoke tests" #7: an agent
1. reads spatial state via `omnilab observe --json`,
2. acts via `omnilab tune <node> --set <p>=<v> --json`,
3. confirms the change via `omnilab inspect --json`.

The full integration runs against a live container on the test machine.
The CI version below exercises the *contract* of each step — that the
predicate engine produces a valid SpatialSummary, the tune planner
produces a sensible YAML patch, and the inspect snapshot can absorb
the resulting state. No subprocess / no podman / no rclpy.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnilab.inspect import SCHEMA_VERSION as INSPECT_SCHEMA
from omnilab.inspect import build_snapshot
from omnilab.inspect_sources import FakeSources
from omnilab.observe import ObserversConfig, ObserversEngine, example_quadruped_state
from omnilab.template import find_repo_templates_dir
from omnilab.tune import ParamSet, build_save_yaml, infer_value_type


def test_agent_loop_round_trip(tmp_path: Path):
    """observe → tune → inspect, all JSON-mode contracts honored."""
    # 1. observe — predicate engine produces a valid SpatialSummary.
    repo_templates = find_repo_templates_dir()
    assert repo_templates is not None
    observers_text = (
        repo_templates.parent / "docs" / "examples" / "observers" / "quadruped.yaml"
    ).read_text()
    config = ObserversConfig.from_yaml(observers_text)
    engine = ObserversEngine(config)
    summary = engine.tick(example_quadruped_state(walking=True))
    summary_json = json.dumps(summary.to_dict())  # JSON-serializable

    payload = json.loads(summary_json)
    assert payload["motion_class"] == "walking_forward"
    assert isinstance(payload["anomalies"], list)
    assert payload["schema_version"] == "1"

    # 2. tune — agent decides to bump max_velocity, emits a `--set` and
    # asks for --save.
    sets = [ParamSet(name="max_velocity", value="0.25"), ParamSet(name="loud", value="true")]
    saved = build_save_yaml(node="/turtlebot3", sets=sets)
    # Round-trips through YAML.
    import yaml

    doc = yaml.safe_load(saved)
    assert doc["/turtlebot3"]["ros__parameters"]["max_velocity"] == 0.25
    assert doc["/turtlebot3"]["ros__parameters"]["loud"] is True

    # 3. inspect — confirms the new state by reading via FakeSources
    # (production: PodmanExecSources). The snapshot's schema_version is
    # stable.
    snap = build_snapshot(FakeSources(), container="proj")
    snap_json = json.dumps(snap.to_json_dict())
    assert json.loads(snap_json)["schema_version"] == INSPECT_SCHEMA


def test_agent_loop_observe_to_tune_decision():
    """Agent reads `falling` → would issue an emergency stop. Verifies
    the predicate engine fires the right anomaly when given a falling-
    quadruped state, and that tune.infer_value_type can produce the
    right command for the action."""
    falling_state = example_quadruped_state(walking=False)
    falling_state["orientation"]["roll"] = 50.0  # past 45 threshold

    text = (
        "motion_classes:\n"
        "  - name: falling\n"
        "    when: \"abs(orientation.roll) > 45 OR abs(orientation.pitch) > 45\"\n"
    )
    engine = ObserversEngine(ObserversConfig.from_yaml(text))
    summary = engine.tick(falling_state)
    assert summary.motion_class == "falling"

    # Agent's reaction: stop. infer_value_type accepts the bool from text.
    assert infer_value_type("false") is False
