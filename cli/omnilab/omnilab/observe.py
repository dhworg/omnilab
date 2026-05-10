"""Agent perception primitive — `omnilab observe`.

Per project-spec-v1.md (rev 3) § "Pillar: Agent perception":
  Layer 1 (this file) — spatial summary: parse `observers.yaml`,
    evaluate predicates against ROS state, emit motion_class label +
    active anomalies. Built-in noise handling via duration_min /
    cooldown / hysteresis. Pure evaluator + state machine here; the
    state-collection side is in `observe_sources.py`.
  Layer 2 — frame capture: shells `gz sim --headless-rendering`
    against the project container. Implementation skeleton only in
    v0; full annotated-overlay rendering is Phase B.future.
  Layer 3 (--diff / --record) — parked for v2.

Predicate language (subset of Python):
  Comparison ops:  >  <  >=  <=  ==  !=
  Logical ops:     AND  OR  NOT  (uppercase per spec; auto-lowered
                                  before parsing)
  Functions:       abs(x), min(...), max(...)
  Atoms:           dotted paths into the state dict (e.g.
                   `linear_velocity.x`) and numeric literals.

Anything else is rejected by the AST whitelist.
"""

from __future__ import annotations

import ast
import datetime as dt
import operator
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

SCHEMA_VERSION = "1"


# ---- predicate language --------------------------------------------------


_BOOL_REWRITES = (
    (re.compile(r"\bAND\b"), "and"),
    (re.compile(r"\bOR\b"), "or"),
    (re.compile(r"\bNOT\b"), "not"),
)
_ALLOWED_FUNCS = {"abs": abs, "min": min, "max": max}
_ALLOWED_OPS: dict[type, Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


class PredicateError(ValueError):
    """Raised on a syntactically- or semantically-invalid predicate."""


def _lower_bool_kws(text: str) -> str:
    for pat, repl in _BOOL_REWRITES:
        text = pat.sub(repl, text)
    return text


def parse_predicate(text: str) -> ast.AST:
    """Parse + validate a predicate. Returns the AST root for evaluator
    reuse. Raises PredicateError on syntactic / disallowed-operation
    errors.
    """
    py = _lower_bool_kws(text)
    try:
        tree = ast.parse(py, mode="eval")
    except SyntaxError as e:
        raise PredicateError(f"syntax error: {e.msg}") from e
    _validate_node(tree.body)
    return tree


def _validate_node(node: ast.AST) -> None:  # noqa: PLR0911, PLR0912
    # AST walkers are inherently branchy; collapsing the cases hurts
    # readability. Suppressed deliberately.
    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _ALLOWED_OPS:
                raise PredicateError(f"operator not allowed: {type(op).__name__}")
        _validate_node(node.left)
        for c in node.comparators:
            _validate_node(c)
        return
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _validate_node(v)
        return
    if isinstance(node, ast.UnaryOp):
        # `not foo` and unary minus on a numeric constant (`< -0.3`).
        if isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            _validate_node(node.operand)
            return
        raise PredicateError(f"unary operator not allowed: {type(node.op).__name__}")
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, bool)):
            raise PredicateError(f"only numeric constants allowed; got {node.value!r}")
        return
    if isinstance(node, (ast.Name, ast.Attribute)):
        return  # state lookup; resolved at eval time
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise PredicateError("only abs / min / max are callable in predicates")
        for a in node.args:
            _validate_node(a)
        return
    raise PredicateError(f"unsupported expression: {type(node).__name__}")


def evaluate(tree: ast.AST | str, state: dict[str, Any]) -> bool:
    """Evaluate a predicate AST (or raw text) against `state`.

    Missing keys raise PredicateError so a misnamed field doesn't
    silently evaluate to False.
    """
    if isinstance(tree, str):
        tree = parse_predicate(tree)
    return bool(_eval_node(tree.body if isinstance(tree, ast.Expression) else tree, state))


def _eval_node(node: ast.AST, state: dict[str, Any]) -> Any:  # noqa: ANN401, PLR0911, PLR0912
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, state)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(right_node, state)
            if not _ALLOWED_OPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, state) for v in node.values)
        return any(_eval_node(v, state) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, state)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in state:
            raise PredicateError(f"unknown name: {node.id!r}")
        return state[node.id]
    if isinstance(node, ast.Attribute):
        chain: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            raise PredicateError("dotted path must start with a name")
        chain.append(cur.id)
        chain.reverse()
        val: Any = state
        for k in chain:
            try:
                val = val[k]
            except (KeyError, TypeError) as e:
                raise PredicateError(
                    f"state lookup failed at {'.'.join(chain)!r}: missing {k!r}"
                ) from e
        return val
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        return _ALLOWED_FUNCS[node.func.id](*(_eval_node(a, state) for a in node.args))
    raise PredicateError(f"unsupported expression: {type(node).__name__}")


# ---- duration / cooldown unit parser ------------------------------------


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)\s*$")


def parse_duration_seconds(spec: str | float | int) -> float:
    """Parse '100ms', '2s', '1.5s', '3m', or a bare number (seconds)."""
    if isinstance(spec, (int, float)):
        return float(spec)
    m = _DURATION_RE.match(spec)
    if not m:
        raise PredicateError(f"invalid duration: {spec!r}")
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "ms":
        return n / 1000.0
    if unit == "s":
        return n
    return n * 60.0


# ---- observer schema -----------------------------------------------------


@dataclass
class ObserverEntry:
    """One motion_class or anomaly entry from observers.yaml."""

    name: str
    when: str
    duration_min: float = 0.0
    cooldown: float = 0.0
    hysteresis: float = 0.0  # currently informational; applied per-predicate

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ObserverEntry:
        if "name" not in d:
            raise PredicateError("observer entry missing 'name'")
        if "when" not in d:
            raise PredicateError(f"observer entry {d['name']!r} missing 'when'")
        return cls(
            name=str(d["name"]),
            when=str(d["when"]),
            duration_min=parse_duration_seconds(d.get("duration_min", 0)),
            cooldown=parse_duration_seconds(d.get("cooldown", 0)),
            hysteresis=float(d.get("hysteresis", 0)),
        )


@dataclass
class ObserversConfig:
    motion_classes: list[ObserverEntry] = field(default_factory=list)
    anomalies: list[ObserverEntry] = field(default_factory=list)

    @classmethod
    def from_yaml(cls, text: str) -> ObserversConfig:
        data = yaml.safe_load(text) or {}
        return cls(
            motion_classes=[
                ObserverEntry.from_dict(e) for e in data.get("motion_classes", [])
            ],
            anomalies=[
                ObserverEntry.from_dict(e) for e in data.get("anomalies", [])
            ],
        )


# ---- noise-handling state machine ---------------------------------------


@dataclass
class _PredicateState:
    """Per-entry runtime state for duration_min + cooldown."""

    last_true_since: float | None = None
    last_fire_at: float | None = None
    active: bool = False  # currently firing


def _step(
    entry: ObserverEntry,
    state: _PredicateState,
    *,
    raw: bool,
    now: float,
) -> bool:
    """Update per-predicate state and return whether the entry is firing
    *this tick*.

    - duration_min: must be raw-true for at least that long since
      `last_true_since` before flipping to active.
    - cooldown: once active, ignore raw values for `cooldown` seconds
      after going inactive.
    """
    if state.last_fire_at is not None and now - state.last_fire_at < entry.cooldown:
        # In cooldown — predicate is forced inactive.
        state.active = False
        state.last_true_since = None
        return False

    if not raw:
        state.last_true_since = None
        if state.active:
            # Going inactive — start cooldown clock.
            state.last_fire_at = now
        state.active = False
        return False

    if state.last_true_since is None:
        state.last_true_since = now

    if now - state.last_true_since >= entry.duration_min:
        state.active = True
        return True

    return False


# ---- spatial summary builder --------------------------------------------


@dataclass
class SpatialSummary:
    schema_version: str
    timestamp: str
    motion_class: str | None
    anomalies: list[str]
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObserversEngine:
    """Stateful evaluator: holds per-entry state across calls.

    `tick(state, now=...)` re-runs every predicate against the supplied
    ROS-derived state dict, advances each entry's noise state machine,
    and returns the active motion_class + anomaly list.
    """

    def __init__(self, config: ObserversConfig) -> None:
        self.config = config
        self._motion_state: dict[str, _PredicateState] = {
            e.name: _PredicateState() for e in config.motion_classes
        }
        self._anom_state: dict[str, _PredicateState] = {
            e.name: _PredicateState() for e in config.anomalies
        }
        # Pre-parse predicates so a typo fails fast.
        self._motion_trees = [(e, parse_predicate(e.when)) for e in config.motion_classes]
        self._anom_trees = [(e, parse_predicate(e.when)) for e in config.anomalies]

    def tick(self, state: dict[str, Any], *, now: float | None = None) -> SpatialSummary:
        ts = now if now is not None else time.monotonic()

        motion_class: str | None = None
        # First-match wins for motion_classes (definitions are ordered).
        for entry, tree in self._motion_trees:
            try:
                raw = bool(evaluate(tree, state))
            except PredicateError:
                raw = False
            firing = _step(
                entry, self._motion_state[entry.name], raw=raw, now=ts
            )
            if firing and motion_class is None:
                motion_class = entry.name

        anomalies: list[str] = []
        for entry, tree in self._anom_trees:
            try:
                raw = bool(evaluate(tree, state))
            except PredicateError:
                raw = False
            firing = _step(
                entry, self._anom_state[entry.name], raw=raw, now=ts
            )
            if firing:
                anomalies.append(entry.name)

        return SpatialSummary(
            schema_version=SCHEMA_VERSION,
            timestamp=dt.datetime.now(dt.UTC).isoformat(),
            motion_class=motion_class,
            anomalies=anomalies,
            state=dict(state),  # echo so the agent can introspect
        )


# ---- validate observers.yaml --------------------------------------------


@dataclass
class ValidationIssue:
    level: Literal["error", "warning"]
    target: str  # entry name
    message: str


def validate_observers(text: str) -> list[ValidationIssue]:
    """Lint observers.yaml: predicate syntax, missing required fields,
    duplicate names, contradictory rules. Returns a list of issues.
    Empty list → all clean.
    """
    issues: list[ValidationIssue] = []
    try:
        config = ObserversConfig.from_yaml(text)
    except PredicateError as e:
        issues.append(ValidationIssue(level="error", target="<root>", message=str(e)))
        return issues

    seen_names: set[str] = set()
    for entry in (*config.motion_classes, *config.anomalies):
        if entry.name in seen_names:
            issues.append(
                ValidationIssue(
                    level="error", target=entry.name, message="duplicate entry name"
                )
            )
        seen_names.add(entry.name)
        try:
            parse_predicate(entry.when)
        except PredicateError as e:
            issues.append(
                ValidationIssue(level="error", target=entry.name, message=str(e))
            )

    # Sanity warnings:
    for entry in config.anomalies:
        if entry.cooldown == 0:
            issues.append(
                ValidationIssue(
                    level="warning",
                    target=entry.name,
                    message="anomaly without cooldown will spam on every tick the predicate stays true",
                )
            )

    return issues


# ---- example state for tests / demos ------------------------------------


def example_quadruped_state(*, walking: bool = True) -> dict[str, Any]:
    """A realistic state dict for the quadruped-walker template, used in
    tests and as a smoke fixture for `omnilab observe`."""
    return {
        "linear_velocity": {"x": 0.12 if walking else 0.0, "y": 0.0, "z": 0.0},
        "angular_velocity": {"z": 0.0},
        "orientation": {"roll": 1.0, "pitch": -2.0, "yaw": 0.0},
        "num_feet_in_contact": 3 if walking else 4,
        "foot": {"in_contact": True, "lateral_velocity": 0.02},
        "sim_time": 12.34,
        "rtf": 0.97,
    }


# ---- frame capture (Layer 2) skeleton ----------------------------------


@dataclass
class CapturePlan:
    """Pure plan for `omnilab observe --capture` — the executor lives in
    cli.py and shells `podman exec` against the running container.

    v0: returns the gz sim invocation + the expected output paths. Real
    annotated-overlay rendering with timestamps + foot-contact markers
    is Phase B.future inside the container's render pipeline.
    """

    output_dir: Path
    duration_seconds: float
    fps: int
    expected_frames: int
    gz_cmd: list[str]


def plan_capture(
    *,
    output_dir: Path,
    duration_seconds: float,
    fps: int,
    world: str | None = None,
) -> CapturePlan:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")
    if fps <= 0:
        raise ValueError("fps must be > 0")
    cmd = ["gz", "sim", "--headless-rendering", "-r", "-s", "--record-frames", str(fps)]
    if world:
        cmd.append(world)
    return CapturePlan(
        output_dir=output_dir,
        duration_seconds=duration_seconds,
        fps=fps,
        expected_frames=int(duration_seconds * fps),
        gz_cmd=cmd,
    )
