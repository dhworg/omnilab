"""`omnilab tune` — agent-action complement to `omnilab observe`.

Per project-spec-v1.md (rev 3) § "v1 must-do" #17. Together
observe + tune form the closed agent loop: agent reads state,
adjusts parameters, reads new state, iterates.

v0 surface in this module:
  - `parse_param_set`: parse `--set name=value` strings.
  - `infer_value_type`: heuristic typing — bool/int/float/string.
  - `build_set_argv`: turn a list of ParamSet into the `ros2 param set`
    argvs (multi-set is multiple calls; agent reports per-call result).
  - `build_save_yaml`: pure function turning current params.yaml +
    pending sets into the post-save document; includes a header
    comment with timestamp and change list.
  - `LiveSetSupport` heuristic via `ros2 param describe` output —
    looks for "dynamic_typing: True" / "read_only: false" markers and
    reports whether a node likely honors live changes.

The CLI command (`omnilab tune`) calls into ros2 via podman exec; we
keep the planner pure and let the executor live in cli.py. Tests use
mock subprocess outputs.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

# ---- ParamSet -----------------------------------------------------------


@dataclass
class ParamSet:
    name: str
    value: str  # raw string; typed via infer_value_type at use time

    @classmethod
    def parse(cls, raw: str) -> ParamSet:
        if "=" not in raw:
            raise ValueError(f"--set requires name=value, got {raw!r}")
        name, _, value = raw.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            raise ValueError(f"empty parameter name in {raw!r}")
        return cls(name=name, value=value)


def infer_value_type(text: str) -> bool | int | float | str:
    """Return a typed Python value from a string.

    Order matters: bool first (so `0` doesn't become `False`), then
    int, then float, then string.
    """
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


# ---- live-set support heuristic ----------------------------------------


@dataclass
class LiveSetSupport:
    """Whether a node accepts live `ros2 param set` calls.

    `confidence` is `"high"` when we found explicit dynamic_typing
    markers, `"low"` when we couldn't tell (assume yes). `"none"` means
    we found explicit read-only markers.
    """

    supported: bool
    confidence: str  # "high" | "low" | "none"
    reason: str = ""


def parse_describe_output(text: str) -> LiveSetSupport:
    """Inspect `ros2 param describe <node> <param>` output to guess
    whether the node honors live changes.

    Sample output:
        Parameter name: max_velocity
        Type: double
        Description: Maximum forward velocity in m/s
        Constraints:
          Read only: false
          Min value: 0.0
          Max value: 5.0
    """
    if "Read only: true" in text or "read_only: true" in text.lower():
        return LiveSetSupport(
            supported=False,
            confidence="high",
            reason="parameter descriptor declares read-only",
        )
    if "dynamic_typing: True" in text:
        return LiveSetSupport(
            supported=True, confidence="high", reason="dynamic_typing: True"
        )
    if "Read only: false" in text:
        return LiveSetSupport(
            supported=True, confidence="high", reason="explicit read-only=false"
        )
    return LiveSetSupport(
        supported=True,
        confidence="low",
        reason="no descriptor signal; assuming live changes work",
    )


# ---- argv builders ------------------------------------------------------


def build_set_argv(node: str, sets: list[ParamSet]) -> list[list[str]]:
    """One `ros2 param set` argv per ParamSet. Caller wraps with
    `podman exec <container> bash -lc "source ros && <argv>"`.
    """
    return [
        ["ros2", "param", "set", node, p.name, p.value]
        for p in sets
    ]


# ---- save-to-yaml planner -----------------------------------------------


_NODE_KEY_RE = re.compile(r"^[A-Za-z_/][A-Za-z0-9_/-]*$")


def build_save_yaml(
    *,
    node: str,
    sets: list[ParamSet],
    existing_yaml: str | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Pure: produce the post-save params.yaml content.

    ROS 2 params.yaml shape:
        /node_name:
          ros__parameters:
            param_name: value
            ...

    Adds a header comment block with timestamp + change list above the
    document so a future reader understands what just changed.
    """
    if not _NODE_KEY_RE.match(node):
        raise ValueError(f"invalid node name: {node!r}")

    doc: dict[str, Any] = yaml.safe_load(existing_yaml) if existing_yaml else {}
    if not isinstance(doc, dict):
        doc = {}

    node_block = doc.setdefault(node, {})
    if not isinstance(node_block, dict):
        node_block = {}
        doc[node] = node_block
    params = node_block.setdefault("ros__parameters", {})
    if not isinstance(params, dict):
        params = {}
        node_block["ros__parameters"] = params

    for p in sets:
        params[p.name] = infer_value_type(p.value)

    when = (now or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
    header = [f"# omnilab tune — saved {when}"]
    if sets:
        header.append(f"# changes for {node}:")
        for p in sets:
            header.append(f"#   - {p.name} = {p.value}")
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
    return "\n".join(header) + "\n" + body


# ---- summary dataclass --------------------------------------------------


@dataclass
class TuneResult:
    node: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    saved_path: str | None = None
    live_support: LiveSetSupport | None = None
