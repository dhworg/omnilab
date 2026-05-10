"""Tests for omnilab.tune — pure helpers."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from omnilab.tune import (
    ParamSet,
    build_save_yaml,
    build_set_argv,
    infer_value_type,
    parse_describe_output,
)

# ---- ParamSet ----------------------------------------------------------


def test_param_set_parse_basic():
    p = ParamSet.parse("max_velocity=1.5")
    assert p.name == "max_velocity"
    assert p.value == "1.5"


def test_param_set_parse_strips_whitespace():
    p = ParamSet.parse("  k = v  ")
    assert p.name == "k"
    assert p.value == "v"


def test_param_set_parse_value_with_equals():
    """`--set foo=a=b` should keep the second `=` in the value."""
    p = ParamSet.parse("foo=a=b")
    assert p.name == "foo"
    assert p.value == "a=b"


def test_param_set_parse_missing_equals():
    with pytest.raises(ValueError, match="name=value"):
        ParamSet.parse("just-a-name")


def test_param_set_parse_empty_name():
    with pytest.raises(ValueError, match="empty parameter name"):
        ParamSet.parse("=value")


# ---- infer_value_type ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("true", True),
        ("True", True),
        ("FALSE", False),
        ("0", 0),
        ("42", 42),
        ("-7", -7),
        ("1.5", 1.5),
        ("-2.0", -2.0),
        ("hello", "hello"),
        ("/path/with/slashes", "/path/with/slashes"),
    ],
)
def test_infer_value_type(text, expected):
    assert infer_value_type(text) == expected


def test_infer_value_type_bool_before_int():
    """`true` must be bool, not parsed as identifier-int-fall-through."""
    v = infer_value_type("true")
    assert v is True
    assert not isinstance(v, int) or v is True  # bool is a subclass of int


# ---- build_set_argv ----------------------------------------------------


def test_build_set_argv_one_param():
    sets = [ParamSet(name="x", value="1")]
    argv = build_set_argv("/node", sets)
    assert argv == [["ros2", "param", "set", "/node", "x", "1"]]


def test_build_set_argv_multi_param():
    sets = [
        ParamSet(name="a", value="1"),
        ParamSet(name="b", value="2.0"),
    ]
    argv = build_set_argv("/node", sets)
    assert len(argv) == 2
    assert argv[0][-2:] == ["a", "1"]
    assert argv[1][-2:] == ["b", "2.0"]


# ---- describe-output heuristic -----------------------------------------


def test_parse_describe_read_only():
    out = "Parameter name: foo\nType: double\nConstraints:\n  Read only: true\n"
    s = parse_describe_output(out)
    assert s.supported is False
    assert s.confidence == "high"


def test_parse_describe_read_only_false():
    out = "Constraints:\n  Read only: false\n"
    s = parse_describe_output(out)
    assert s.supported is True
    assert s.confidence == "high"


def test_parse_describe_dynamic_typing():
    out = "Parameter type: PARAMETER_DOUBLE\ndynamic_typing: True\n"
    s = parse_describe_output(out)
    assert s.supported is True
    assert s.confidence == "high"


def test_parse_describe_unknown_assumes_yes():
    s = parse_describe_output("nothing useful")
    assert s.supported is True
    assert s.confidence == "low"


# ---- build_save_yaml ---------------------------------------------------


def test_save_yaml_creates_node_block():
    text = build_save_yaml(
        node="/turtle",
        sets=[ParamSet(name="speed", value="0.5"), ParamSet(name="loud", value="true")],
        now=dt.datetime(2026, 5, 10, 12, 0, 0, tzinfo=dt.UTC),
    )
    # Header comment present.
    assert "# omnilab tune — saved 2026-05-10T12:00:00+00:00" in text
    # Round-trip the YAML.
    doc = yaml.safe_load(text)
    assert doc["/turtle"]["ros__parameters"]["speed"] == 0.5
    assert doc["/turtle"]["ros__parameters"]["loud"] is True


def test_save_yaml_merges_existing():
    existing = (
        "/turtle:\n"
        "  ros__parameters:\n"
        "    speed: 0.1\n"
        "    other: 42\n"
    )
    text = build_save_yaml(
        node="/turtle",
        sets=[ParamSet(name="speed", value="0.5")],
        existing_yaml=existing,
    )
    doc = yaml.safe_load(text)
    # Updated.
    assert doc["/turtle"]["ros__parameters"]["speed"] == 0.5
    # Preserved.
    assert doc["/turtle"]["ros__parameters"]["other"] == 42


def test_save_yaml_change_list_in_header():
    text = build_save_yaml(
        node="/x",
        sets=[ParamSet(name="a", value="1"), ParamSet(name="b", value="2")],
    )
    assert "# changes for /x:" in text
    assert "#   - a = 1" in text
    assert "#   - b = 2" in text


def test_save_yaml_rejects_bad_node_name():
    with pytest.raises(ValueError, match="invalid node name"):
        build_save_yaml(node="not a name!", sets=[ParamSet(name="x", value="1")])
