"""Typer app: `omnilab new | up | down | sim | doctor` (+ `version`).

Honors project-spec-v1.md (rev 3) § "CLI conventions":
- Dual-mode output via `--json` root flag (see `_output.py`).
- Destructive commands accept `--dry-run` + `--yes` (see `_safety.py`).
- Documented exit codes:
    0 success, 1 generic, 2 invalid args, 3 state, 4 network, 5 permission.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import asdict, replace
from importlib import resources
from pathlib import Path

import typer
import yaml

from . import __version__, _output, _safety
from .gpu import detect_gpu, resolve_gpu_mode
from .manifest import OmnilabManifest, PairConfig, write_pair_config
from .podman import (
    build_run_args,
    container_running,
    detect_host_context,
    exec_in,
    has_podman,
    run,
    stop_container,
)

app = typer.Typer(
    help="OmniLab — robotics dev environment manager.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def root(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON instead of human text/TUI.",
        is_eager=True,
    ),
) -> None:
    """Sets the global output mode before any subcommand runs."""
    _output.set_json_mode(json_output)


def _load_manifest(project_dir: Path) -> OmnilabManifest:
    """Load + validate omnilab.yaml. Exits with code 3 on missing/invalid."""
    manifest_path = project_dir / "omnilab.yaml"
    if not manifest_path.exists():
        _output.emit_error(
            f"no omnilab.yaml in {project_dir}. Run `omnilab new <name>` first.",
            code=3,
            project_dir=str(project_dir),
        )
    try:
        return OmnilabManifest.from_yaml(manifest_path)
    except Exception as e:  # noqa: BLE001
        _output.emit_error(
            f"invalid {manifest_path}: {e}",
            code=3,
            manifest_path=str(manifest_path),
        )
        raise  # unreachable; emit_error raises Exit


@app.command()
def version() -> None:
    """Print omnilab CLI version."""
    _output.emit(human=f"omnilab {__version__}", data={"version": __version__})


@app.command()
def new(
    name: str = typer.Argument(..., help="Project name (alnum + dash/underscore)."),
    template: str = typer.Option(
        "ros-jazzy-gz-harmonic",
        "--template",
        "-t",
        help="Project template to use.",
    ),
    directory: Path | None = typer.Option(
        None,
        "--directory",
        "-d",
        help="Where to create the project (default: ./<name>).",
    ),
) -> None:
    """Scaffold a new OmniLab project directory."""
    target = directory if directory is not None else Path.cwd() / name
    if target.exists():
        _output.emit_error(
            f"{target} already exists.",
            code=2,
            target=str(target),
        )

    try:
        template_text = (
            resources.files("omnilab.templates").joinpath(f"{template}.yaml").read_text()
        )
    except FileNotFoundError:
        _output.emit_error(f"unknown template '{template}'", code=2, template=template)

    rendered = template_text.replace("{name}", name)

    OmnilabManifest.model_validate(yaml.safe_load(rendered))

    target.mkdir(parents=True)
    manifest_path = target / "omnilab.yaml"
    manifest_path.write_text(rendered)

    _output.emit(
        human=(
            f"Created project at {target}\n"
            "Next steps:\n"
            f"  cd {target}\n"
            "  omnilab up\n"
            "  omnilab sim"
        ),
        data={
            "project": name,
            "path": str(target),
            "manifest_path": str(manifest_path),
            "template": template,
        },
    )


@app.command()
def up(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Start the project container with podman."""
    if not has_podman():
        _output.emit_error("podman not installed or not on PATH.", code=5)

    manifest = _load_manifest(project_dir)

    if container_running(manifest.name):
        _output.emit(
            human=f"Container '{manifest.name}' is already running.",
            data={"container": manifest.name, "status": "already_running"},
        )
        return

    ctx = detect_host_context(project_dir)
    ctx = replace(ctx, gpu=resolve_gpu_mode(manifest.gpu))

    args = build_run_args(manifest, ctx, detach=True)
    _output.emit(human=f"Starting {manifest.name} (gpu={ctx.gpu})…")
    result = run(args)
    if result.returncode != 0:
        _output.emit_error(
            f"podman run failed:\n{result.stderr}",
            code=1,
            container=manifest.name,
            stderr=result.stderr,
        )

    # Ensure Wayland-Qt deps are present in the container so Gazebo's
    # GUI doesn't crash on first launch. Project image SHOULD ship
    # these (Phase B.5+ — file as ros-jazzy-gz-harmonic Dockerfile
    # change). Idempotent: skips if already installed.
    _ensure_wayland_qt_deps(manifest.name)

    _output.emit(
        human=f"Container '{manifest.name}' is up.",
        data={"container": manifest.name, "status": "started", "gpu": ctx.gpu},
    )


def _ensure_wayland_qt_deps(container_name: str) -> None:
    """Install qtwayland5 + wayland-utils inside the container if missing.
    Apt-installed packages don't survive container recreate, so this
    runs on every `omnilab up`. The proper fix is baking them into the
    project image; this is the bridge until that lands.
    """
    import subprocess

    check = subprocess.run(
        ["podman", "exec", container_name, "bash", "-c",
         "dpkg -l qtwayland5 2>/dev/null | grep -q '^ii'"],
        capture_output=True, text=True, check=False,
    )
    if check.returncode == 0:
        return  # Already installed.
    _output.emit(human="Installing Wayland-Qt deps in container (one-time)…")
    install = subprocess.run(
        ["podman", "exec", "-u", "0", container_name, "bash", "-c",
         "apt-get update -qq && apt-get install -y -qq "
         "qtwayland5 wayland-utils > /tmp/.apt.log 2>&1"],
        capture_output=True, text=True, check=False,
    )
    if install.returncode != 0:
        _output.emit(
            human=(
                f"warning: failed to install Wayland deps "
                f"(see /tmp/.apt.log inside container): rc={install.returncode}"
            ),
        )


@app.command()
def down(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview the action; do not stop the container."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Stop the project container (destructive — confirms by default)."""
    if not has_podman():
        _output.emit_error("podman not installed or not on PATH.", code=5)

    manifest = _load_manifest(project_dir)

    if not container_running(manifest.name):
        _output.emit(
            human=f"Container '{manifest.name}' is not running.",
            data={"container": manifest.name, "status": "not_running"},
        )
        return

    _safety.confirm_or_exit(
        summary=f"Stop container '{manifest.name}'?",
        items=[f"podman stop {manifest.name}"],
        yes=yes,
        dry_run=dry_run,
        json_payload={"container": manifest.name, "action": "stop"},
    )

    result = stop_container(manifest.name)
    if result.returncode != 0:
        _output.emit_error(
            f"stop failed:\n{result.stderr}",
            code=1,
            container=manifest.name,
            stderr=result.stderr,
        )
    _output.emit(
        human=f"Container '{manifest.name}' stopped.",
        data={"container": manifest.name, "status": "stopped"},
    )


@app.command()
def sim(
    headless: bool = typer.Option(False, "--headless", help="Run sim without GUI."),
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Launch the project's simulator in the running container.

    gazebo (default): the demo TurtleBot3 + nav2 simulation.
    mujoco: the model from `mujoco.model`, via `mujoco.bridge` when set
    (which publishes /clock + /joint_states for observe/inspect/record)
    or the bare viewer when not.
    """
    manifest = _load_manifest(project_dir)
    if not container_running(manifest.name):
        _output.emit_error(
            f"Container '{manifest.name}' is not running. Run `omnilab up` first.",
            code=3,
            container=manifest.name,
        )

    try:
        launch = sim_launch_command(manifest, headless=headless)
    except ValueError as e:
        _output.emit_error(str(e), code=2)
    cmd = ["bash", "-lc", launch]
    rc = exec_in(manifest.name, cmd)
    raise typer.Exit(rc)


def sim_launch_command(manifest: OmnilabManifest, *, headless: bool = False) -> str:
    """Pure: the in-container launch line for `omnilab sim`.

    Dispatches on manifest.simulator. Raises ValueError for a mujoco
    project with no model configured — that's a manifest problem the user
    can fix, not a state error.
    """
    if manifest.simulator == "mujoco":
        m = manifest.mujoco
        if not m.model:
            raise ValueError(
                "simulator is mujoco but no model is set — add `mujoco:\n  model: <path.xml>` "
                "to omnilab.yaml (path relative to the project dir)"
            )
        if m.bridge:
            # The bridge steps physics and publishes /clock + /joint_states,
            # which is what observe/inspect/record consume.
            line = f"source /opt/ros/jazzy/setup.bash && python3 /workspace/{m.bridge} --model /workspace/{m.model}"
            if headless:
                line += " --headless"
            return line
        if headless:
            raise ValueError(
                "headless mujoco needs a bridge script (the bare viewer is GUI-only) — "
                "add `mujoco:\n  bridge: <script.py>` to omnilab.yaml"
            )
        # Viewer-only fallback: physics runs, nothing is published to ROS.
        return f"python3 -m mujoco.viewer --mjcf=/workspace/{m.model}"

    line = (
        "source /opt/ros/jazzy/setup.bash && "
        "TURTLEBOT3_MODEL=burger ros2 launch nav2_bringup tb3_simulation_launch.py"
    )
    if headless:
        line += " headless:=True"
    return line


@app.command()
def tune(
    node: str = typer.Argument(..., help="ROS node to tune (e.g. /turtlebot3_diff_drive)."),
    sets: list[str] = typer.Option(
        [],
        "--set",
        help="Parameter to set as `name=value`. Repeatable; applied in order.",
    ),
    save: bool = typer.Option(
        False, "--save", help="Persist the changes to params.yaml in the project dir."
    ),
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Live ROS parameter set + save (agent-action complement to observe)."""
    from . import tune as tunemod

    if not sets:
        _output.emit_error(
            "no --set <name>=<value> provided; nothing to tune.", code=2
        )

    try:
        parsed = [tunemod.ParamSet.parse(s) for s in sets]
    except ValueError as e:
        _output.emit_error(str(e), code=2)

    manifest = _load_manifest(project_dir)
    if not container_running(manifest.name):
        _output.emit_error(
            f"container '{manifest.name}' is not running. Run `omnilab up` first.",
            code=3,
        )

    # Live-set support heuristic — describe the first param.
    describe = run(
        [
            "podman",
            "exec",
            manifest.name,
            "bash",
            "-lc",
            f"source /opt/ros/jazzy/setup.bash && ros2 param describe {node} {parsed[0].name}",
        ]
    )
    live = tunemod.parse_describe_output(describe.stdout)

    applied: list[dict] = []
    failed: list[dict] = []
    for argv in tunemod.build_set_argv(node, parsed):
        cmd_str = " ".join(argv)
        result = run(
            [
                "podman",
                "exec",
                manifest.name,
                "bash",
                "-lc",
                f"source /opt/ros/jazzy/setup.bash && {cmd_str}",
            ]
        )
        record = {"argv": argv, "rc": result.returncode, "stderr": result.stderr.strip()}
        (applied if result.returncode == 0 else failed).append(record)

    saved_path: str | None = None
    if save:
        params_yaml = project_dir / "params.yaml"
        existing = params_yaml.read_text() if params_yaml.exists() else None
        rendered = tunemod.build_save_yaml(
            node=node, sets=parsed, existing_yaml=existing
        )
        params_yaml.write_text(rendered)
        saved_path = str(params_yaml)

    result = tunemod.TuneResult(
        node=node, applied=applied, failed=failed, saved_path=saved_path, live_support=live
    )
    _output.emit(
        human=(
            f"node: {node}\n"
            f"  live-set support: {live.supported} ({live.confidence}: {live.reason})\n"
            f"  applied: {len(applied)}, failed: {len(failed)}"
            + (f"\n  saved to {saved_path}" if saved_path else "")
        ),
        data={
            "node": result.node,
            "applied": result.applied,
            "failed": result.failed,
            "saved_path": result.saved_path,
            "live_support": asdict(result.live_support) if result.live_support else None,
        },
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def observe(  # noqa: PLR0912, PLR0915
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    no_capture: bool = typer.Option(
        False, "--no-capture",
        help="DEBUGGING ONLY: skip the Layer 2 screenshot. The output's "
             "motion_class will be moved to `numeric_motion_class` with "
             "`verification_mode: \"numeric_only\"`. Numeric signatures "
             "can match physical states that aren't actually true (a bot "
             "wedged sideways with jammed knees reads the same as a "
             "standing bot). Always pair observe with the screenshot on "
             "a live sim for any claim about robot physical state.",
    ),
    duration: float = typer.Option(
        2.0, "--duration", help="Frame capture window length in seconds."
    ),
    fps: int = typer.Option(10, "--fps", help="Frame rate for capture."),
    validate: Path | None = typer.Option(
        None, "--validate", help="Lint an observers.yaml; do not collect state."
    ),
) -> None:
    """Agent perception primitive — Layer 1 spatial summary + Layer 2 visual.

    ⚠ DANGER ⚠ — Numeric-only verdicts are unverified.

    Against a live sim, this command captures BOTH the numeric ROS
    state AND a screenshot of Gazebo, then evaluates predicates. The
    JSON output's verdict field is named based on whether the visual
    was actually verified:

      - `verified_motion_class` — image was captured AND fresh. Safe
        for an agent to quote as a claim about the robot's physical
        state.
      - `numeric_motion_class` — image was skipped (--no-capture) or
        failed to write. The signature matches a class but no visual
        cross-check happened. Do NOT quote this as a claim about the
        robot; it can match "robot standing" while the robot is
        actually wedged at an angle, buried in mesh, or off-camera.

    The default is --capture on. --no-capture is for debugging the
    predicate engine itself, not for normal observation.
    """
    # Cross-modal verification is on by default; --no-capture opts out.
    capture: bool = not no_capture
    from . import observe as obs

    if validate is not None:
        text = validate.read_text()
        issues = obs.validate_observers(text)
        if issues:
            for i in issues:
                _output.emit(human=f"  [{i.level}] {i.target}: {i.message}")
            errors = sum(1 for i in issues if i.level == "error")
            _output.emit(
                human=f"\n{errors} error(s), {len(issues) - errors} warning(s)",
                data={"issues": [asdict(i) for i in issues], "errors": errors},
            )
            raise typer.Exit(1 if errors else 0)
        _output.emit(human="observers.yaml is clean.", data={"issues": []})
        return

    # Project-presence detection. When there's no omnilab.yaml here, the
    # spec contract (verification #1) says: source=example, canned state,
    # empty observers, exit 0 — agent still gets a valid snapshot.
    manifest_path = project_dir / "omnilab.yaml"
    if not manifest_path.exists():
        import sys
        print(
            f"warning: no omnilab.yaml in {project_dir} — using example "
            "state with no observers (agent gets a baseline snapshot)",
            file=sys.stderr,
        )
        empty_config = obs.ObserversConfig()
        engine = obs.ObserversEngine(empty_config)
        summary = engine.tick(
            obs.example_quadruped_state(),
            source="example",
            verification_mode="no_image_source",
            low_confidence_reason="No project / no live container / canned example state.",
            project=None,
            container=None,
        )
        _output.emit(
            human=(
                f"source:       {summary.source}\n"
                f"numeric_motion_class:   {summary.numeric_motion_class}  (⚠ {summary.verification_mode})\n"
                f"anomalies:    {', '.join(a.name for a in summary.anomalies) or '—'}"
            ),
            data=summary.to_dict(),
        )
        return

    manifest = _load_manifest(project_dir)

    # Frame capture paths differ per simulator. Gazebo: gz GUI/world
    # services (pause-capture-resume). MuJoCo: the bridge script serves a
    # file-based capture protocol from inside the sim process — same-step
    # state+image, so visual verification works there too. What CAN'T
    # capture is a mujoco project with no bridge (the bare viewer renders
    # to a window, not to us) — that degrades to the numeric path.
    sim_supports_capture = manifest.simulator == "gazebo" or (
        manifest.simulator == "mujoco" and manifest.mujoco.bridge is not None
    )
    if capture and not sim_supports_capture:
        capture = False

    if manifest.observers is None:
        _output.emit_error(
            "no observers: key in omnilab.yaml — add `observers: observers.yaml`",
            code=3,
        )
    observers_path = project_dir / manifest.observers
    if not observers_path.exists():
        _output.emit_error(f"observers file not found: {observers_path}", code=3)

    config = obs.ObserversConfig.from_yaml(observers_path.read_text())
    engine = obs.ObserversEngine(config)

    # Try live state from the project's running container first; fall
    # back to canned example state when no container is up (spec
    # behavior contract #3) or exit 3 on broken container state (#4).
    from . import observe_sources as srcs

    container_name = manifest.name
    if manifest.simulator == "mujoco":
        src = srcs.MujocoLiveSource(container_name, project_dir=project_dir)
    else:
        src = srcs.LiveStateSource(container_name)
    status = src.status()

    state: dict
    source_label: str = "example"
    live_status_payload: dict | None = None
    pre_capture_result = None

    calibration_payload: dict | None = None
    if status.state == "running":
        if capture:
            # Apply world-frame camera framing once before the first
            # capture of this session (task #22 — option B). Camera
            # position + look-at are absolute in world frame, so the
            # framing is unambiguous regardless of how the spawned
            # entity is rotated.
            from . import observe_derived as _derived_mod
            _cam_cfg = _derived_mod.DerivedConfig.load(project_dir)
            if _cam_cfg.camera_world_position_xyz:
                src.apply_camera_pose(
                    _cam_cfg.camera_world_position_xyz,
                    _cam_cfg.camera_look_at_xyz,
                )

            # Atomic pause-capture-resume — state and image MUST come
            # from the same simulator instant (feedback_calibration_simultaneity.md).
            # No more "screenshot first then state later" — that was the
            # bug class that let me conflate a freefall-screenshot with
            # a settled-numbers reading. Now they're locked together.
            host_capture_dir = project_dir / ".omnilab" / "captures"
            cal = src.calibrated_sample(host_capture_dir)
            sample = cal.state_result
            pre_capture_result = cal.capture_result
            calibration_payload = {
                "method": cal.calibration_method,
                "calibrated": cal.calibrated,
                "state_sim_time_s": cal.state_sim_time_s,
                "image_sim_time_s": cal.image_sim_time_s,
                "sim_time_skew_s": cal.sim_time_skew_s,
                "error": cal.calibration_error,
            }
        else:
            # --no-capture path: still use the legacy non-paused sample
            # since there's no image to align to.
            sample = src.sample()

        state = sample.state
        source_label = "live"
        live_status_payload = {
            "container": container_name,
            "topics_seen": sample.topics_seen,
            "topics_missing": sample.topics_missing,
            "collect_window_seconds": sample.collect_window_seconds,
            "state_sample_started_at": sample.state_sample_started_at,
            "state_sample_ended_at": sample.state_sample_ended_at,
        }
        if sample.raw_error:
            # Live source returned but errored mid-collection.
            live_status_payload["raw_error"] = sample.raw_error
            # Still proceed with whatever state we got (might be empty).
    elif status.state == "missing":
        # No container for this project — fall back to example state.
        import sys
        print(
            f"warning: no running container for project '{container_name}' — "
            "using example state (run `omnilab up` to use live data)",
            file=sys.stderr,
        )
        state = obs.example_quadruped_state()
        source_label = "example"
    else:
        # exited / paused / created / unknown / etc. — error per contract #4.
        _output.emit_error(
            f"container '{container_name}' is in state '{status.state}' "
            f"(expected 'running' or 'missing'). "
            f"Run `omnilab clean -d {project_dir} --yes` and `omnilab up -d {project_dir}` to reset.",
            code=3,
        )
        return  # _output.emit_error raises typer.Exit; this is just for type-checker

    # Determine verification_mode. The bar for "verified" is now
    # atomic simultaneity (feedback_calibration_simultaneity.md):
    # state and image must come from the same simulator instant. A
    # successful capture isn't enough on its own anymore.
    verification_mode: str
    low_confidence_reason: str | None = None
    if source_label == "example":
        verification_mode = "no_image_source"
        low_confidence_reason = (
            "Source is canned example state — no live image exists to verify "
            "against. Verdict is informational only."
        )
    elif not capture and not sim_supports_capture:
        # Not a user choice — nothing in this project can render a frame.
        verification_mode = "no_image_source"
        low_confidence_reason = (
            "simulator is mujoco with no bridge configured — the bare viewer "
            "renders to a window, not to us, so there is no image to verify "
            "against. Add `mujoco:\n  bridge: <script.py>` for visual "
            "verification. Do NOT quote this verdict as a visually verified "
            "claim about robot physical state."
        )
    elif not capture:
        verification_mode = "numeric_only"
        low_confidence_reason = (
            "--no-capture was passed. The numeric signature matches a class "
            "but no visual cross-check happened. Numeric signatures can fire "
            "spuriously (bot wedged sideways, body in mesh, off-camera). Do "
            "NOT quote this verdict as a claim about robot physical state."
        )
    elif pre_capture_result is None or pre_capture_result.frame_path is None:
        # We tried to capture but it failed (stale PNG, service lied, etc.)
        verification_mode = "image_failed"
        err = pre_capture_result.error if pre_capture_result else "no result"
        low_confidence_reason = (
            f"Screenshot capture failed: {err}. Numeric signature is "
            "available but unverified. Restart gz GUI or use the rescue "
            "path in CLAUDE.md before trusting this verdict."
        )
    elif calibration_payload is None or not calibration_payload.get("calibrated", False):
        # Image captured, but state and image are NOT from the same
        # simulator instant. Per feedback_calibration_simultaneity.md,
        # this means the derived class is unverified — the image is of
        # one moment, the numbers are of another.
        verification_mode = "calibration_failed"
        cal_err = (calibration_payload or {}).get("error") or "unknown"
        skew = (calibration_payload or {}).get("sim_time_skew_s")
        skew_ms = f"{skew*1000:.1f}ms" if isinstance(skew, (int, float)) else "?"
        low_confidence_reason = (
            f"Calibration failed: image and state sim_times diverged "
            f"(skew={skew_ms}). Reason: {cal_err}. The image and the "
            "state numbers describe different physical instants; no "
            "derived class can be verified against the screenshot."
        )
    else:
        verification_mode = "verified"

    # Compute the derived semantic layer if we have live state. Inherits
    # the calibrated sample's sim_time so derived fields are tied to the
    # same physical instant as state+image (see observe_derived.py).
    derived_payload: dict | None = None
    if source_label == "live":
        from . import observe_derived as derived_mod

        derived_cfg = derived_mod.DerivedConfig.load(project_dir)
        sim_t = (calibration_payload or {}).get("state_sim_time_s")
        history = derived_mod.read_recent_history(project_dir, n=2)
        derived_payload = derived_mod.compute_derived(
            state, derived_cfg, sim_time_s=sim_t, history=history,
        )

        # Append THIS sample to history for future motion-rate computations.
        # Only persist if calibration was good — don't pollute history
        # with un-trustworthy samples.
        if calibration_payload and calibration_payload.get("calibrated"):
            record = derived_mod.build_history_record(
                sim_time_s=sim_t,
                state=state,
                image_path=(pre_capture_result.frame_path
                            if pre_capture_result else None),
                verification_mode=verification_mode,
            )
            # History persistence is best-effort; never fail observe on it.
            with contextlib.suppress(OSError):
                derived_mod.append_history(project_dir, record)

    summary = engine.tick(
        state,
        source=source_label,  # type: ignore[arg-type]
        verification_mode=verification_mode,  # type: ignore[arg-type]
        low_confidence_reason=low_confidence_reason,
        project=manifest.name,
        container=container_name if source_label == "live" else None,
        live_status=live_status_payload,
        calibration=calibration_payload,
        derived=derived_payload,
    )

    capture_payload: dict | None = None
    if capture:
        # Real Layer 2: a multimodal agent can Read the PNG and "see"
        # the scene. Catches failure modes pure-numeric observe misses
        # (freefall, mesh penetration, off-camera, etc.).
        # We took the screenshot BEFORE sample() so its timestamp lines
        # up with the start of the state collection window. The
        # captured_at + state_sample_started_at lets the agent verify
        # they're from the same physical moment.
        if pre_capture_result is not None:
            capture_payload = {
                "frame_path": pre_capture_result.frame_path,
                "captured_at": pre_capture_result.captured_at,
                "error": pre_capture_result.error,
            }
        else:
            capture_payload = {
                "frame_path": None,
                "captured_at": None,
                "error": "no live container; --capture requires running sim",
            }

    # In human-readable output, name the verdict field after its
    # verification status so a human reading the CLI doesn't make the
    # same mistake the JSON schema is designed to prevent.
    if summary.verification_mode == "verified":
        verdict_line = f"motion_class:           {summary.verified_motion_class}  (verified)"
    else:
        verdict_line = (
            f"numeric_motion_class:   {summary.numeric_motion_class}  "
            f"(⚠ {summary.verification_mode})"
        )
    _output.emit(
        human=(
            f"source:       {summary.source}\n"
            f"{verdict_line}\n"
            f"anomalies:    {', '.join(a.name for a in summary.anomalies) or '—'}"
        ),
        data={
            **summary.to_dict(),
            **({"capture": capture_payload} if capture_payload else {}),
        },
    )


template_app = typer.Typer(help="Manage starter templates for new projects.")
app.add_typer(template_app, name="template")


def _local_registry():
    from . import template as tmpl

    root = tmpl.find_repo_templates_dir()
    if root is None:
        _output.emit_error(
            "no templates/ directory found above cwd. Run from inside the OmniLab repo "
            "or set up an OCI registry source (Phase B.future).",
            code=3,
        )
    return tmpl.LocalRegistry(root)


@template_app.command("list")
def template_list() -> None:
    """List available templates."""
    from . import template as tmpl

    registry = _local_registry()
    names = registry.list_names()
    items = []
    for n in names:
        info_text = (registry.fetch(n) / "template.yaml").read_text()
        info = tmpl.TemplateInfo.from_yaml(info_text)
        items.append(asdict(info))
    _output.emit(
        human="\n".join(f"{i['name']:<20}  {i['description'].splitlines()[0]}" for i in items)
        or "(no templates)",
        data={"templates": items},
    )


@template_app.command("show")
def template_show(name: str) -> None:
    """Print a template's metadata + files list."""
    from . import template as tmpl

    registry = _local_registry()
    try:
        path = registry.fetch(name)
    except tmpl.TemplateNotFound:
        _output.emit_error(f"template not found: {name}", code=3, name=name)
    info = tmpl.TemplateInfo.from_yaml((path / "template.yaml").read_text())
    _output.emit(
        human=f"{info.name} (v{info.version})\n  {info.description}\nVariables: {info.variables}\nFiles: {info.files}",
        data=asdict(info),
    )


@template_app.command("install")
def template_install(
    name: str = typer.Argument(...),
    project_name: str | None = typer.Option(
        None, "--project-name", help="Override project_name variable (default: cwd basename)."
    ),
    target: Path | None = typer.Option(
        None, "--target", help="Where to install (default: cwd)."
    ),
) -> None:
    """Install a template into the current project."""
    from . import template as tmpl

    registry = _local_registry()
    try:
        path = registry.fetch(name)
    except tmpl.TemplateNotFound:
        _output.emit_error(f"template not found: {name}", code=3, name=name)
    info = tmpl.TemplateInfo.from_yaml((path / "template.yaml").read_text())
    target = target or Path.cwd()
    proj_name = project_name or target.resolve().name
    variables = {"project_name": proj_name}
    try:
        written = tmpl.install_template(
            info=info, template_root=path, target=target, variables=variables
        )
    except FileExistsError as e:
        _output.emit_error(str(e), code=2)
    _output.emit(
        human=f"Installed {info.name} into {target} ({len(written)} files).",
        data={
            "template": info.name,
            "target": str(target),
            "project_name": proj_name,
            "files_written": [str(p) for p in written],
        },
    )


pair_app = typer.Typer(help="LAN-first peer pairing for ROS DDS.")
app.add_typer(pair_app, name="pair")


@pair_app.command("init")
def pair_init(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Generate a memorable pairing code; print it for the peer to use."""
    from . import pair as pairmod

    code = pairmod.generate_pairing_code()
    domain_id = pairmod.derive_domain_id(code)
    _output.emit(
        human=(
            f"Pairing code: {code}\n"
            f"  derived ROS_DOMAIN_ID: {domain_id}\n"
            "Share the code with your peer and run `omnilab pair join <code>` on both machines."
        ),
        data={"code": code, "domain_id": domain_id},
    )


@pair_app.command("join")
def pair_join(
    code: str = typer.Argument(..., help="Pairing code from `omnilab pair init`."),
    peer_ip: str | None = typer.Option(
        None, "--peer-ip", help="Peer IP if not auto-discoverable."
    ),
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Probe network, pick RMW mode, write Cyclone DDS config, persist."""
    from . import pair as pairmod

    if not pairmod.is_valid_pairing_code(code):
        _output.emit_error(f"invalid pairing code: {code!r}", code=2)

    domain_id = pairmod.derive_domain_id(code)
    interface = pairmod.default_interface()
    local_ip = pairmod.local_ip_for(interface)

    probe = pairmod.NetworkProbe(
        peer_reachable=(peer_ip is not None and pairmod.probe_peer_reachable(peer_ip)),
        can_multicast=True,  # v0 assumes; deeper probe is Phase B.future
        nat_detected=False,
        interface=interface,
        local_ip=local_ip,
        peer_ip=peer_ip,
    )
    mode = pairmod.select_pairing_mode(probe)

    if mode is None:
        _output.emit_error(
            pairmod.UNREACHABLE_PEER_HINT,
            code=4,
            code_attempted=code,
            peer_ip=peer_ip,
        )

    xml_dir = project_dir / ".omnilab"
    xml_dir.mkdir(parents=True, exist_ok=True)
    xml_path = xml_dir / "cyclonedds.xml"
    xml_path.write_text(
        pairmod.cyclonedds_xml(
            domain_id=domain_id,
            mode=mode,
            interface=interface,
            peer_ip=peer_ip,
        )
    )

    backend = pairmod.detect_firewall_backend()
    fw_cmds = pairmod.firewall_commands(domain_id=domain_id, backend=backend)

    # Persist the pairing into omnilab.yaml. Without this, `omnilab up`
    # reads ros.domain_id and starts the container on the wrong DDS
    # partition — the XML above gets written and then ignored, which is
    # indistinguishable from pairing having done nothing at all.
    manifest_written = False
    if (project_dir / "omnilab.yaml").exists():
        write_pair_config(
            project_dir,
            PairConfig(domain_id=domain_id, config=mode),
        )
        manifest_written = True

    result = pairmod.PairResult(
        code=code,
        domain_id=domain_id,
        mode=mode,
        interface=interface,
        local_ip=local_ip,
        peer_ip=peer_ip,
        cyclonedds_xml_path=str(xml_path),
        firewall_backend=backend,
    )
    manifest_line = (
        f"  omnilab.yaml: pair.domain_id={domain_id} written\n"
        if manifest_written
        else "  omnilab.yaml: not found — run `omnilab pair join` from the project dir\n"
    )
    _output.emit(
        human=(
            f"Paired. mode={mode}, domain_id={domain_id}, iface={interface}\n"
            f"  Cyclone DDS config: {xml_path}\n"
            f"{manifest_line}"
            f"  Firewall backend: {backend} ({len(fw_cmds)} rules to apply)\n"
            "  Restart the container for this to take effect: `omnilab down && omnilab up`"
        ),
        data={
            **result.to_dict(),
            "firewall_commands": fw_cmds,
            "manifest_updated": manifest_written,
        },
    )


@pair_app.command("status")
def pair_status(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Report current pairing state (XML present? domain_id active?)."""
    xml_path = project_dir / ".omnilab" / "cyclonedds.xml"
    paired = xml_path.exists()
    data = {"paired": paired}
    if paired:
        text = xml_path.read_text()
        m = re.search(r"<Domain\s+id='(\d+)'>", text)
        if m:
            data["domain_id"] = int(m.group(1))
    _output.emit(
        human="paired" if paired else "not paired",
        data=data,
    )


@app.command()
def record(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    name: str | None = typer.Option(
        None, "--name", help="Override the auto-generated recording id."
    ),
    duration: float | None = typer.Option(
        None, "--duration", help="Auto-stop after this many seconds."
    ),
    topics: list[str] | None = typer.Option(
        None, "--topics", help="Whitelist topics (repeatable). Disables default exclusions."
    ),
    with_cameras: bool = typer.Option(
        False, "--with-cameras", help="Don't exclude camera image / depth topics."
    ),
    with_screencast: bool = typer.Option(
        False, "--with-screencast", help="Capture wf-recorder screencast alongside the bag."
    ),
    start_background: bool = typer.Option(
        False, "--start", help="Start a background recording. Pair with --background."
    ),
    background: bool = typer.Option(
        False, "--background", help="Daemonize the recorder; print id and return."
    ),
    stop: str | None = typer.Option(
        None, "--stop", help="Stop a previously-started background recording by id."
    ),
) -> None:
    """Smart bag recording with metadata sidecar (per spec § Recording)."""
    from . import record as recmod

    manifest = _load_manifest(project_dir)
    mgr = recmod.RecordingManager(project_dir)

    if stop:
        rc = mgr.stop_background(stop)
        meta = mgr.load_metadata(stop)
        _output.emit(
            human=f"Stopped recording {stop} (duration={meta.duration_seconds:.1f}s).",
            data={"recording_id": stop, "metadata": asdict(meta), "return_code": rc},
        )
        raise typer.Exit(rc)

    if not container_running(manifest.name):
        _output.emit_error(
            f"container '{manifest.name}' is not running. Run `omnilab up` first.",
            code=3,
            container=manifest.name,
        )

    rec_id = name or recmod.auto_recording_id(manifest.name)
    excluded: list[str] = list(recmod.DEFAULT_EXCLUDE_PATTERNS)
    if not with_cameras:
        excluded.extend(recmod.CAMERA_EXCLUDE_PATTERNS)

    metadata = recmod.RecordingMetadata(
        schema_version=recmod.SCHEMA_VERSION,
        recording_id=rec_id,
        project=manifest.name,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
        image=manifest.image,
        manifest_digest=recmod.hash_file(project_dir / "omnilab.yaml"),
        observers_hash=recmod.hash_file(project_dir / "observers.yaml"),
        topics_excluded=excluded,
        topics_whitelist=topics,
    )

    rec_dir = mgr.init_recording(recording_id=rec_id, metadata=metadata)

    if with_screencast and recmod.detect_screencast_tool() is None:
        _output.emit(
            human="WARNING: --with-screencast requested but wf-recorder is not installed; bag-only recording.",
        )

    bag_args = recmod.build_record_args(
        bag_dir=rec_dir / "bag",
        topics_whitelist=topics,
        excluded_patterns=excluded if not topics else None,
    )

    _output.emit(
        human=(
            f"Recording id={rec_id}\n"
            f"  bag dir: {rec_dir}/bag\n"
            f"  topics:  {'whitelist=' + ','.join(topics) if topics else 'all minus exclusions'}\n"
            f"  background: {start_background and background}"
        ),
        data={
            "recording_id": rec_id,
            "path": str(rec_dir),
            "metadata": asdict(metadata),
            "argv": bag_args,
            "background": bool(start_background and background),
            "duration_limit_seconds": duration,
        },
    )


@app.command()
def replay(
    recording: str = typer.Argument(..., help="Recording id or path."),
    rate: float | None = typer.Option(None, "--rate", help="Playback rate multiplier."),
    start_offset: float | None = typer.Option(
        None, "--start-offset", help="Skip N seconds from the start."
    ),
    loop: bool = typer.Option(False, "--loop", help="Loop playback."),
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Replay a recorded bag. Warns on environment mismatch."""
    from . import record as recmod

    manifest = _load_manifest(project_dir)
    mgr = recmod.RecordingManager(project_dir)
    meta = mgr.load_metadata(recording)

    warnings = recmod.env_mismatch_warnings(meta, current_image=manifest.image)
    args = recmod.build_replay_args(
        bag_dir=mgr.recordings_dir / recording / "bag",
        rate=rate,
        start_offset=start_offset,
        loop=loop,
    )

    _output.emit(
        human=("\n".join(f"⚠  {w}" for w in warnings) if warnings else "Env OK.")
        + f"\nReplay argv: {' '.join(args)}",
        data={
            "recording_id": recording,
            "metadata": asdict(meta),
            "warnings": warnings,
            "argv": args,
        },
    )


@app.command()
def clean(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    all_projects: bool = typer.Option(
        False, "--all", help="NUCLEAR — clean every omnilab-labeled container, not just current project."
    ),
    aggressive: bool = typer.Option(
        False,
        "--aggressive",
        help="Walk process trees (children-first) and SIGTERM→SIGKILL them. D-state procs are still only reported.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan; take no action."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
) -> None:
    """Safe orphan / leftover-state cleanup. Container-kill primitives, D-state honest."""
    from . import clean as cleanmod

    project: str | None = None
    if not all_projects:
        manifest = _load_manifest(project_dir)
        project = manifest.name

    procs = cleanmod.read_proc_snapshot()
    containers = cleanmod.read_container_snapshot()
    # For each RUNNING in-scope container, also read its inside-process
    # list so the planner can detect duplicate sim processes. Fixes the
    # "omnilab clean says nothing to clean while two gz sims are
    # visibly running" bug (task #21).
    for c in containers:
        if c.state == "running" and (
            all_projects or c.project == project
        ):
            c.inside_procs = cleanmod.read_inside_container_procs(c.name)
    plan = cleanmod.plan_cleanup(
        project=project,
        containers=containers,
        procs=procs,
        all_projects=all_projects,
        aggressive=aggressive,
    )

    summary_lines = []
    if plan.is_empty():
        _output.emit(
            human="Nothing to clean.",
            data={**plan.to_dict(), "result": "noop"},
        )
        return

    summary_lines.append(
        f"Would act on {len(plan.actions)} target(s)"
        f" (scope={plan.scope}, aggressive={aggressive})."
    )
    if plan.d_state_processes:
        summary_lines.append(
            f"⚠ {len(plan.d_state_processes)} D-state (uninterruptible) "
            "process(es) — these CANNOT be killed; reboot may be required."
        )

    items = [f"{a.kind}: {a.target} ({a.reason})" for a in plan.actions]
    if plan.d_state_processes:
        items.extend(
            f"D-state pid={p.pid} {p.name} (REBOOT)" for p in plan.d_state_processes
        )

    _safety.confirm_or_exit(
        summary="\n".join(summary_lines),
        items=items,
        yes=yes,
        dry_run=dry_run,
        json_payload=plan.to_dict(),
    )

    results = cleanmod.execute_plan(plan)
    failed = sum(1 for _, rc in results if rc != 0)
    _output.emit(
        human=f"Cleanup complete. {len(results) - failed} ok, {failed} failed.",
        data={
            "result": "executed",
            "succeeded": len(results) - failed,
            "failed": failed,
            "actions": [{**a.to_dict(), "return_code": rc} for a, rc in results],
            "d_state_processes": [asdict(p) for p in plan.d_state_processes],
        },
    )
    if failed:
        raise typer.Exit(1)


@app.command()
def inspect(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    refresh: float = typer.Option(
        1.0, "--refresh", min=0.1, max=10.0, help="TUI refresh rate in Hz."
    ),
) -> None:
    """Live unified dashboard — nodes, topics, services, TF, Gazebo.

    Read-only. Default human mode is a Textual TUI that refreshes at
    `--refresh` Hz; `--json` returns a single structured snapshot.
    """
    manifest = _load_manifest(project_dir)
    if not container_running(manifest.name):
        _output.emit_error(
            f"container '{manifest.name}' is not running. Run `omnilab up` first.",
            code=3,
            container=manifest.name,
        )

    from .inspect import build_snapshot
    from .inspect_sources import PodmanExecSources

    sources = PodmanExecSources(manifest.name, simulator=manifest.simulator)

    if _output.is_json_mode():
        snapshot = build_snapshot(sources, container=manifest.name)
        _output.emit(data=snapshot.to_json_dict())
        return

    from .inspect_tui import run_tui

    rc = run_tui(sources, container=manifest.name, refresh_hz=refresh)
    raise typer.Exit(rc)


@app.command()
def gpu(
    fix: bool = typer.Option(
        False, "--fix", help="Apply every auto-fixable repair, in ladder order."
    ),
    wake: bool = typer.Option(
        True, "--wake/--no-wake", help="Query nvidia-smi first, which resumes a suspended GPU."
    ),
    container: bool = typer.Option(
        False,
        "--container",
        help="Also verify end-to-end by running nvidia-smi + glxinfo inside the project image.",
    ),
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    tui: bool = typer.Option(True, "--tui/--no-tui", help="Interactive UI (human mode only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With --fix, print commands without running them."),
    yes: bool = typer.Option(False, "--yes", help="With --fix, skip the confirmation prompt."),
) -> None:
    """Diagnose and repair NVIDIA GPU passthrough.

    The common failure is not a missing driver — it's a driver that's
    installed while nothing uses it: a suspended dGPU, an unloaded
    `nvidia_uvm`, missing device nodes, a stale CDI spec, or rendering
    silently landing on llvmpipe. This walks that ladder from the PCI bus
    outward and fixes what it can.
    """
    from . import gpu_doctor

    probe = gpu_doctor.probe_host(wake=wake)

    if container:
        manifest = _load_manifest(project_dir)
        smi_ok, renderer = gpu_doctor.probe_container(manifest.image)
        probe.container_smi_ok = smi_ok
        probe.container_renderer = renderer

    checks = gpu_doctor.evaluate(probe)
    summary = gpu_doctor.summarize(checks)

    if fix:
        fixable = gpu_doctor.autofixable(checks)
        if not fixable:
            _output.emit(
                human="Nothing to auto-fix.",
                data={"summary": summary, "checks": [c.to_dict() for c in checks], "applied": []},
            )
            return

        _safety.confirm_or_exit(
            summary=f"Apply {len(fixable)} GPU fix(es):",
            items=[f"{c.title} — {c.fix.description}" for c in fixable if c.fix],
            yes=yes,
            dry_run=dry_run,
            json_payload={"planned": [c.to_dict() for c in fixable]},
        )

        applied: list[dict] = []
        for check in fixable:
            if check.fix is None:
                continue
            ok, lines = gpu_doctor.apply_fix(check.fix, dry_run=dry_run)
            applied.append({"key": check.key, "ok": ok, "transcript": lines})
            if not _output.is_json_mode():
                typer.echo(f"→ {check.fix.description}")
                for line in lines:
                    typer.echo(f"   {line}")
            if not ok:
                break

        # Re-probe so the caller sees the post-fix state, not the plan.
        probe = gpu_doctor.probe_host(wake=wake)
        checks = gpu_doctor.evaluate(probe)
        summary = gpu_doctor.summarize(checks)
        _output.emit(
            human="\n" + gpu_tui_report(checks),
            data={"summary": summary, "checks": [c.to_dict() for c in checks], "applied": applied},
        )
        raise typer.Exit(0 if summary["overall"] != "fail" else 3)

    if _output.is_json_mode():
        _output.emit(data={"summary": summary, "checks": [c.to_dict() for c in checks]})
        raise typer.Exit(0 if summary["overall"] != "fail" else 3)

    if tui:
        # NOTE: typer.Exit subclasses RuntimeError, so the TUI call must sit
        # outside any `except RuntimeError` or the exit code gets swallowed.
        rc: int | None = None
        try:
            from .gpu_tui import run_tui

            rc = run_tui(initial=probe)
        except RuntimeError as e:
            # textual missing — fall through to the plain report rather
            # than failing a diagnostic command over a UI dependency.
            typer.echo(f"({e})\n", err=True)
        if rc is not None:
            raise typer.Exit(rc)

    typer.echo(gpu_tui_report(checks))
    raise typer.Exit(0 if summary["overall"] != "fail" else 3)


def gpu_tui_report(checks: list) -> str:
    """Plain-text report — no textual import, safe when the extra is absent."""
    from .gpu_tui import format_report

    return format_report(checks)


@app.command()
def doctor(  # noqa: PLR0912, PLR0915
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Run extended checks (templates, observers, pair, recordings).",
    ),
) -> None:
    """Health check: podman, GPU, image pullable, manifest valid (+ extended with --full)."""
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    # --- environment ---
    add("podman on PATH", has_podman(), "from $PATH")
    gpu = detect_gpu()
    add(f"GPU detected: {gpu}", gpu != "none")

    # --- manifest ---
    manifest_path = project_dir / "omnilab.yaml"
    manifest = None
    if not manifest_path.exists():
        add("omnilab.yaml present", False, f"none at {manifest_path} — try `omnilab new`")
    else:
        try:
            manifest = OmnilabManifest.from_yaml(manifest_path)
            add(f"omnilab.yaml parses (project={manifest.name})", True)
        except Exception as e:  # noqa: BLE001
            add("omnilab.yaml parses", False, str(e))

    # --- image ---
    if manifest is not None:
        if has_podman():
            # podman manifest inspect works for multi-arch manifest lists
            # but errors on single-arch images with "Treating single
            # images as manifest lists is not implemented" — which means
            # the image IS pullable, just not a manifest list. Fall back
            # to `podman image inspect` (locally available = pullable).
            rc = run(["podman", "manifest", "inspect", manifest.image])
            ok = rc.returncode == 0
            detail = "manifest fetched"
            if not ok:
                if "single images as manifest lists" in (rc.stderr or ""):
                    # Single-arch image, locally pulled. Check that.
                    rc_local = run(["podman", "image", "inspect", manifest.image])
                    ok = rc_local.returncode == 0
                    detail = (
                        "single-arch image, locally available"
                        if ok else "single-arch image, not pulled locally"
                    )
                else:
                    # Other error — truncate stderr to avoid 200-line dumps.
                    first_line = (rc.stderr or "").strip().splitlines()
                    msg = first_line[0] if first_line else "unknown error"
                    detail = msg[:120] + ("…" if len(msg) > 120 else "")
            add(f"image '{manifest.image}' pullable", ok, detail)
        else:
            add("image reachability", False, "skipped (no podman)")

    # --- extended checks (--full) -----------------------------------------
    if full:
        from . import observe as obsmod
        from . import pair as pairmod
        from . import record as recmod
        from . import template as tmplmod

        # Templates
        templates_root = tmplmod.find_repo_templates_dir(project_dir)
        if templates_root is None:
            add("templates registry", False, "no templates/ dir found above project_dir")
        else:
            reg = tmplmod.LocalRegistry(templates_root)
            names = reg.list_names()
            add(
                f"templates available: {len(names)}",
                len(names) > 0,
                ", ".join(names[:5]),
            )

        # Observers
        if manifest and manifest.observers:
            obs_path = project_dir / manifest.observers
            if not obs_path.exists():
                add(f"observers file {manifest.observers}", False, "missing")
            else:
                issues = obsmod.validate_observers(obs_path.read_text())
                errors = [i for i in issues if i.level == "error"]
                add(
                    f"observers.yaml lints clean ({len(issues)} issues)",
                    len(errors) == 0,
                    f"{len(errors)} errors" if errors else "ok",
                )
        else:
            add("observers.yaml configured", False, "no `observers:` key in manifest")

        # Pair
        pair_xml = project_dir / ".omnilab" / "cyclonedds.xml"
        add("pair config present", pair_xml.exists(), str(pair_xml) if pair_xml.exists() else "run `omnilab pair`")

        # Recordings
        mgr = recmod.RecordingManager(project_dir)
        rec_count = len(mgr.list_recordings())
        add(f"recordings on disk: {rec_count}", True)

        # Firewall backend (informational)
        backend = pairmod.detect_firewall_backend()
        add(f"firewall backend: {backend}", True, "informational")

    passed = sum(1 for c in checks if c["ok"])
    failed = sum(1 for c in checks if not c["ok"])

    if _output.is_json_mode():
        _output.emit(
            data={
                "passed": passed,
                "failed": failed,
                "full": full,
                "checks": checks,
            },
        )
    else:
        typer.echo("=== environment ===")
        for c in checks[:2]:
            _print_check(c)
        typer.echo("\n=== manifest ===")
        for c in checks[2:3]:
            _print_check(c)
        if any("image" in str(c["name"]) for c in checks):
            typer.echo("\n=== image ===")
            for c in checks:
                if "image" in str(c["name"]):
                    _print_check(c)
        if full:
            typer.echo("\n=== extended ===")
            for c in checks:
                if not any(k in str(c["name"]) for k in ("podman", "GPU", "omnilab.yaml", "image")):
                    _print_check(c)
        typer.echo(f"\nResult: {passed} passed, {failed} failed.")

    raise typer.Exit(failed)


def _print_check(c: dict[str, object]) -> None:
    marker = _output.style_pass() if c["ok"] else _output.style_fail()
    line = f"  {marker} {c['name']}"
    if c.get("detail"):
        line += f"  ({c['detail']})"
    typer.echo(line)
