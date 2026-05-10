"""Typer app: `omnilab new | up | down | sim | doctor` (+ `version`).

Honors project-spec-v1.md (rev 3) § "CLI conventions":
- Dual-mode output via `--json` root flag (see `_output.py`).
- Destructive commands accept `--dry-run` + `--yes` (see `_safety.py`).
- Documented exit codes:
    0 success, 1 generic, 2 invalid args, 3 state, 4 network, 5 permission.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from importlib import resources
from pathlib import Path

import typer
import yaml

from . import __version__, _output, _safety
from .gpu import detect_gpu, resolve_gpu_mode
from .manifest import OmnilabManifest
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
    _output.emit(
        human=f"Container '{manifest.name}' is up.",
        data={"container": manifest.name, "status": "started", "gpu": ctx.gpu},
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
    """Launch the demo TurtleBot3 + nav2 simulation in the running container."""
    manifest = _load_manifest(project_dir)
    if not container_running(manifest.name):
        _output.emit_error(
            f"Container '{manifest.name}' is not running. Run `omnilab up` first.",
            code=3,
            container=manifest.name,
        )

    launch = (
        "source /opt/ros/jazzy/setup.bash && "
        "TURTLEBOT3_MODEL=burger ros2 launch nav2_bringup tb3_simulation_launch.py"
    )
    if headless:
        launch += " headless:=True"
    cmd = ["bash", "-lc", launch]
    rc = exec_in(manifest.name, cmd)
    raise typer.Exit(rc)


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

    sources = PodmanExecSources(manifest.name)

    if _output.is_json_mode():
        snapshot = build_snapshot(sources, container=manifest.name)
        _output.emit(data=snapshot.to_json_dict())
        return

    from .inspect_tui import run_tui

    rc = run_tui(sources, container=manifest.name, refresh_hz=refresh)
    raise typer.Exit(rc)


@app.command()
def doctor(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Health check: podman, GPU, image pullable, manifest valid."""
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    # --- environment ---
    add("podman on PATH", has_podman(), "from $PATH")
    gpu = detect_gpu()
    add(f"GPU detected: {gpu}", gpu != "none")

    # --- manifest ---
    manifest_path = project_dir / "omnilab.yaml"
    if not manifest_path.exists():
        add("omnilab.yaml present", False, f"none at {manifest_path} — try `omnilab new`")
        manifest = None
    else:
        try:
            manifest = OmnilabManifest.from_yaml(manifest_path)
            add(f"omnilab.yaml parses (project={manifest.name})", True)
        except Exception as e:  # noqa: BLE001
            manifest = None
            add("omnilab.yaml parses", False, str(e))

    # --- image ---
    if manifest is not None:
        if has_podman():
            rc = run(["podman", "manifest", "inspect", manifest.image])
            ok = rc.returncode == 0
            detail = "manifest fetched" if ok else (rc.stderr.strip().splitlines() or [""])[0]
            add(f"image '{manifest.image}' pullable", ok, detail)
        else:
            add("image reachability", False, "skipped (no podman)")

    passed = sum(1 for c in checks if c["ok"])
    failed = sum(1 for c in checks if not c["ok"])

    if _output.is_json_mode():
        _output.emit(
            data={"passed": passed, "failed": failed, "checks": checks},
        )
    else:
        # Human-style sectioned output.
        typer.echo("=== environment ===")
        for c in checks[:2]:
            _print_check(c)
        typer.echo("\n=== manifest ===")
        for c in checks[2:3]:
            _print_check(c)
        if len(checks) > 3:
            typer.echo("\n=== image ===")
            for c in checks[3:]:
                _print_check(c)
        typer.echo(f"\nResult: {passed} passed, {failed} failed.")

    raise typer.Exit(failed)


def _print_check(c: dict[str, object]) -> None:
    marker = _output.style_pass() if c["ok"] else _output.style_fail()
    line = f"  {marker} {c['name']}"
    if c.get("detail"):
        line += f"  ({c['detail']})"
    typer.echo(line)
