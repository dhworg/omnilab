"""Tests for omnilab.observe — predicate language, noise state machine, validator."""

from __future__ import annotations

import textwrap

import pytest

from omnilab.observe import (
    ObserverEntry,
    ObserversConfig,
    ObserversEngine,
    PredicateError,
    _PredicateState,
    _step,
    evaluate,
    example_quadruped_state,
    parse_duration_seconds,
    parse_predicate,
    plan_capture,
    validate_observers,
)

# ---- predicate parsing ---------------------------------------------------


def test_parse_simple_comparison():
    parse_predicate("x > 1")


def test_parse_with_uppercase_logical_kws():
    parse_predicate("a > 1 AND b < 2 OR NOT c == 3")


def test_parse_dotted_path():
    parse_predicate("foo.bar.baz > 0")


def test_parse_abs_call():
    parse_predicate("abs(orientation.roll) > 45")


def test_parse_rejects_arithmetic():
    """Arithmetic ops (+, -, *) aren't in the spec's predicate grammar."""
    with pytest.raises(PredicateError):
        parse_predicate("x + 1 > 2")


def test_parse_rejects_function_calls_outside_whitelist():
    with pytest.raises(PredicateError, match="abs / min / max"):
        parse_predicate("os.system('rm') == 0")


def test_parse_rejects_string_literals():
    with pytest.raises(PredicateError, match="numeric"):
        parse_predicate("x == 'hello'")


# ---- predicate evaluation -----------------------------------------------


def test_eval_simple_comparison():
    assert evaluate("x > 1", {"x": 2}) is True
    assert evaluate("x > 1", {"x": 1}) is False


def test_eval_dotted_path():
    state = {"linear_velocity": {"x": 0.1}}
    assert evaluate("linear_velocity.x > 0.05", state) is True


def test_eval_and_or_not():
    state = {"a": 1, "b": 0, "c": 1}
    assert evaluate("a > 0 AND b > 0", state) is False
    assert evaluate("a > 0 OR b > 0", state) is True
    assert evaluate("NOT (b > 0)", state) is True


def test_eval_abs():
    assert evaluate("abs(x) > 5", {"x": -7}) is True
    assert evaluate("abs(x) > 5", {"x": 3}) is False


def test_eval_unknown_path_raises():
    with pytest.raises(PredicateError, match="missing"):
        evaluate("foo.bar > 0", {"foo": {}})


def test_eval_unknown_name_raises():
    with pytest.raises(PredicateError, match="unknown name"):
        evaluate("nonexistent > 0", {})


# ---- duration parser ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100ms", 0.1),
        ("500ms", 0.5),
        ("1s", 1.0),
        ("1.5s", 1.5),
        ("2m", 120.0),
        (3, 3.0),
        (0.5, 0.5),
    ],
)
def test_parse_duration_units(raw, expected):
    assert parse_duration_seconds(raw) == pytest.approx(expected)


def test_parse_duration_invalid():
    with pytest.raises(PredicateError):
        parse_duration_seconds("3hours")


# ---- noise state machine ------------------------------------------------


def test_step_fires_immediately_when_no_duration():
    entry = ObserverEntry(name="x", when="t == 1")
    state = _PredicateState()
    assert _step(entry, state, raw=True, now=0.0) is True
    assert _step(entry, state, raw=True, now=0.1) is True


def test_step_waits_for_duration_min_before_firing():
    entry = ObserverEntry(name="x", when="t == 1", duration_min=0.5)
    state = _PredicateState()
    assert _step(entry, state, raw=True, now=0.0) is False  # just started
    assert _step(entry, state, raw=True, now=0.3) is False  # not yet
    assert _step(entry, state, raw=True, now=0.5) is True


def test_step_resets_duration_on_false():
    entry = ObserverEntry(name="x", when="t == 1", duration_min=0.5)
    state = _PredicateState()
    _step(entry, state, raw=True, now=0.0)
    _step(entry, state, raw=False, now=0.3)
    # Restart the duration clock.
    assert _step(entry, state, raw=True, now=0.4) is False
    assert _step(entry, state, raw=True, now=0.9) is True


def test_step_cooldown_suppresses_refire():
    entry = ObserverEntry(name="x", when="t == 1", cooldown=1.0)
    state = _PredicateState()
    assert _step(entry, state, raw=True, now=0.0) is True
    # Goes inactive; cooldown clock starts.
    _step(entry, state, raw=False, now=0.5)
    # Even though raw is true again, cooldown blocks.
    assert _step(entry, state, raw=True, now=0.7) is False
    # Past cooldown — fires again.
    assert _step(entry, state, raw=True, now=2.0) is True


# ---- ObserversConfig parsing --------------------------------------------


def test_observers_config_from_yaml():
    text = textwrap.dedent(
        """\
        motion_classes:
          - name: walking
            when: "v.x > 0"
        anomalies:
          - name: slip
            when: "foot.lateral_velocity > 0.1"
            duration_min: 50ms
            cooldown: 500ms
        """
    )
    config = ObserversConfig.from_yaml(text)
    assert len(config.motion_classes) == 1
    assert len(config.anomalies) == 1
    slip = config.anomalies[0]
    assert slip.duration_min == pytest.approx(0.05)
    assert slip.cooldown == pytest.approx(0.5)


def test_observers_config_missing_when_raises():
    with pytest.raises(PredicateError, match="missing 'when'"):
        ObserversConfig.from_yaml("motion_classes:\n  - name: foo\n")


# ---- engine round-trip ---------------------------------------------------


def test_engine_classifies_walking_quadruped():
    text = textwrap.dedent(
        """\
        motion_classes:
          - name: walking_forward
            when: "linear_velocity.x > 0.05 AND num_feet_in_contact >= 2"
          - name: standing
            when: "abs(linear_velocity.x) < 0.02"
        anomalies:
          - name: foot_slip
            when: "foot.in_contact AND foot.lateral_velocity > 0.1"
        """
    )
    config = ObserversConfig.from_yaml(text)
    engine = ObserversEngine(config)
    summary = engine.tick(example_quadruped_state(walking=True))
    assert summary.motion_class == "walking_forward"
    assert summary.anomalies == []


def test_engine_classifies_standing_quadruped():
    text = textwrap.dedent(
        """\
        motion_classes:
          - name: walking
            when: "linear_velocity.x > 0.05"
          - name: standing
            when: "abs(linear_velocity.x) < 0.02 AND num_feet_in_contact == 4"
        """
    )
    engine = ObserversEngine(ObserversConfig.from_yaml(text))
    summary = engine.tick(example_quadruped_state(walking=False))
    assert summary.motion_class == "standing"


def test_engine_anomaly_fires_then_cools_down():
    text = textwrap.dedent(
        """\
        anomalies:
          - name: slip
            when: "foot.lateral_velocity > 0.1"
            cooldown: 1s
        """
    )
    engine = ObserversEngine(ObserversConfig.from_yaml(text))
    fast = {"foot": {"in_contact": True, "lateral_velocity": 0.5}}
    slow = {"foot": {"in_contact": True, "lateral_velocity": 0.0}}

    s1 = engine.tick(fast, now=0.0)
    assert "slip" in s1.anomalies

    # Goes inactive — cooldown clock starts.
    engine.tick(slow, now=0.5)

    # Cooldown blocks re-fire.
    s_block = engine.tick(fast, now=0.7)
    assert "slip" not in s_block.anomalies

    # Past cooldown.
    s_again = engine.tick(fast, now=2.0)
    assert "slip" in s_again.anomalies


# ---- validator ----------------------------------------------------------


def test_validate_clean_yaml():
    text = textwrap.dedent(
        """\
        motion_classes:
          - name: walking
            when: "v.x > 0"
        anomalies:
          - name: slip
            when: "foot.lateral_velocity > 0.1"
            cooldown: 500ms
        """
    )
    issues = validate_observers(text)
    assert issues == []


def test_validate_catches_syntax_error():
    text = "motion_classes:\n  - name: x\n    when: '> 1 AND foo'\n"
    issues = validate_observers(text)
    assert any(i.level == "error" for i in issues)


def test_validate_catches_duplicate_name():
    text = textwrap.dedent(
        """\
        motion_classes:
          - name: x
            when: "a > 0"
        anomalies:
          - name: x
            when: "b > 0"
            cooldown: 500ms
        """
    )
    issues = validate_observers(text)
    assert any("duplicate" in i.message for i in issues)


def test_validate_warns_on_anomaly_without_cooldown():
    text = textwrap.dedent(
        """\
        anomalies:
          - name: spammy
            when: "x > 0"
        """
    )
    issues = validate_observers(text)
    assert any(i.level == "warning" and "cooldown" in i.message for i in issues)


# ---- capture plan -------------------------------------------------------


def test_plan_capture_basic(tmp_path):
    plan = plan_capture(output_dir=tmp_path / "caps", duration_seconds=2.0, fps=10)
    assert plan.expected_frames == 20
    assert "gz" in plan.gz_cmd
    assert "--headless-rendering" in plan.gz_cmd


def test_plan_capture_rejects_zero_duration():
    with pytest.raises(ValueError):
        plan_capture(output_dir=__import__("pathlib").Path("/tmp"), duration_seconds=0, fps=10)


def test_plan_capture_rejects_zero_fps():
    with pytest.raises(ValueError):
        plan_capture(
            output_dir=__import__("pathlib").Path("/tmp"), duration_seconds=1.0, fps=0
        )


# ---- example observers files in docs/ -----------------------------------


def test_shipped_example_observers_parse():
    """The three example observers files in docs/examples/observers/
    must validate cleanly."""
    from omnilab.template import find_repo_templates_dir

    repo_root = find_repo_templates_dir()
    assert repo_root is not None
    docs = repo_root.parent / "docs" / "examples" / "observers"
    for fname in ("quadruped.yaml", "mobile_2d.yaml", "arm_6dof.yaml"):
        text = (docs / fname).read_text()
        issues = validate_observers(text)
        assert not [i for i in issues if i.level == "error"], (
            f"{fname}: {[i.message for i in issues if i.level == 'error']}"
        )
