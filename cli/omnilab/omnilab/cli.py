"""Typer app: `omnilab new | up | down | sim | doctor` (+ `version`).

Honors project-spec-v1.md (rev 3) § "CLI conventions":
- Dual-mode output via `--json` root flag (see `_output.py`).
- Destructive commands accept `--dry-run` + `--yes` (see `_safety.py`).
- Documented exit codes:
    0 success, 1 generic, 2 invalid args, 3 state, 4 network, 5 permission.
"""

from __future__ import annotations

import re
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
def observe(
    project_dir: Path = typer.Option(
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
    capture: bool = typer.Option(
        False, "--capture", help="Layer 2: capture annotated frames during the window."
    ),
    duration: float = typer.Option(
        2.0, "--duration", help="Capture window length in seconds."
    ),
    fps: int = typer.Option(10, "--fps", help="Frame rate for --capture."),
    validate: Path | None = typer.Option(
        None, "--validate", help="Lint an observers.yaml; do not collect state."
    ),
) -> None:
    """Agent perception primitive — Layer 1 spatial summary, Layer 2 frame capture."""
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

    manifest = _load_manifest(project_dir)
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

    # v0 uses the example state when no live container is up so the
    # snapshot still demos the predicate engine. When a container is
    # running, future versions will pull live state from
    # PodmanExecSources (Phase B.future).
    state = obs.example_quadruped_state()
    summary = engine.tick(state)

    capture_payload: dict | None = None
    if capture:
        plan = obs.plan_capture(
            output_dir=project_dir / ".omnilab" / "captures",
            duration_seconds=duration,
            fps=fps,
        )
        capture_payload = {
            "output_dir": str(plan.output_dir),
            "duration_seconds": plan.duration_seconds,
            "fps": plan.fps,
            "expected_frames": plan.expected_frames,
            "gz_cmd": plan.gz_cmd,
        }

    _output.emit(
        human=(
            f"motion_class: {summary.motion_class or '—'}\n"
            f"anomalies:    {', '.join(summary.anomalies) or '—'}"
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
    _output.emit(
        human=(
            f"Paired. mode={mode}, domain_id={domain_id}, iface={interface}\n"
            f"  Cyclone DDS config: {xml_path}\n"
            f"  Firewall backend: {backend} ({len(fw_cmds)} rules to apply)"
        ),
        data={**result.to_dict(), "firewall_commands": fw_cmds},
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
            rc = run(["podman", "manifest", "inspect", manifest.image])
            ok = rc.returncode == 0
            detail = "manifest fetched" if ok else (rc.stderr.strip().splitlines() or [""])[0]
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
