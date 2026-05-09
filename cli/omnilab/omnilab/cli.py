"""Typer app: `omnilab new | up | down | sim | doctor`."""

from __future__ import annotations

from dataclasses import replace
from importlib import resources
from pathlib import Path

import typer
import yaml

from . import __version__
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


def _load_manifest(project_dir: Path) -> OmnilabManifest:
    manifest_path = project_dir / "omnilab.yaml"
    if not manifest_path.exists():
        typer.echo(
            f"ERROR: no omnilab.yaml in {project_dir}. Run `omnilab new <name>` first.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        return OmnilabManifest.from_yaml(manifest_path)
    except Exception as e:  # noqa: BLE001
        typer.echo(f"ERROR: invalid {manifest_path}: {e}", err=True)
        raise typer.Exit(1) from e


@app.command()
def version() -> None:
    """Print omnilab CLI version."""
    typer.echo(f"omnilab {__version__}")


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
        typer.echo(f"ERROR: {target} already exists.", err=True)
        raise typer.Exit(1)

    # Load template from package data.
    try:
        template_text = (
            resources.files("omnilab.templates").joinpath(f"{template}.yaml").read_text()
        )
    except FileNotFoundError as e:
        typer.echo(f"ERROR: unknown template '{template}'", err=True)
        raise typer.Exit(1) from e

    rendered = template_text.replace("{name}", name)

    # Validate the rendered manifest before writing — catches bad names early.
    OmnilabManifest.model_validate(yaml.safe_load(rendered))

    target.mkdir(parents=True)
    (target / "omnilab.yaml").write_text(rendered)
    typer.echo(f"Created project at {target}")
    typer.echo("Next steps:")
    typer.echo(f"  cd {target}")
    typer.echo("  omnilab up")
    typer.echo("  omnilab sim")


@app.command()
def up(
    project_dir: Path = typer.Option(  # noqa: B008
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Start the project container with podman."""
    if not has_podman():
        typer.echo("ERROR: podman not installed or not on PATH.", err=True)
        raise typer.Exit(2)

    manifest = _load_manifest(project_dir)

    if container_running(manifest.name):
        typer.echo(f"Container '{manifest.name}' is already running.")
        return

    ctx = detect_host_context(project_dir)
    # Override gpu kind from manifest (manifest 'auto' → host detect).
    ctx = replace(ctx, gpu=resolve_gpu_mode(manifest.gpu))

    args = build_run_args(manifest, ctx, detach=True)
    typer.echo(f"Starting {manifest.name} (gpu={ctx.gpu})…")
    result = run(args)
    if result.returncode != 0:
        typer.echo(f"ERROR: podman run failed:\n{result.stderr}", err=True)
        raise typer.Exit(result.returncode)
    typer.echo(f"Container '{manifest.name}' is up.")


@app.command()
def down(
    project_dir: Path = typer.Option(  # noqa: B008
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Stop the project container."""
    if not has_podman():
        typer.echo("ERROR: podman not installed or not on PATH.", err=True)
        raise typer.Exit(2)
    manifest = _load_manifest(project_dir)
    if not container_running(manifest.name):
        typer.echo(f"Container '{manifest.name}' is not running.")
        return
    result = stop_container(manifest.name)
    if result.returncode != 0:
        typer.echo(f"ERROR: stop failed:\n{result.stderr}", err=True)
        raise typer.Exit(result.returncode)
    typer.echo(f"Container '{manifest.name}' stopped.")


@app.command()
def sim(
    headless: bool = typer.Option(False, "--headless", help="Run sim without GUI."),
    project_dir: Path = typer.Option(  # noqa: B008
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Launch the demo TurtleBot3 + nav2 simulation in the running container."""
    manifest = _load_manifest(project_dir)
    if not container_running(manifest.name):
        typer.echo(
            f"Container '{manifest.name}' is not running. Run `omnilab up` first.",
            err=True,
        )
        raise typer.Exit(1)

    # Use the demo nav2 + TurtleBot3 launch the project image already has
    # via ros-jazzy-nav2-bringup + ros-jazzy-turtlebot3*.
    if headless:
        cmd = [
            "bash",
            "-lc",
            (
                "source /opt/ros/jazzy/setup.bash && "
                "TURTLEBOT3_MODEL=burger ros2 launch nav2_bringup tb3_simulation_launch.py "
                "headless:=True"
            ),
        ]
    else:
        cmd = [
            "bash",
            "-lc",
            (
                "source /opt/ros/jazzy/setup.bash && "
                "TURTLEBOT3_MODEL=burger ros2 launch nav2_bringup tb3_simulation_launch.py"
            ),
        ]
    rc = exec_in(manifest.name, cmd)
    raise typer.Exit(rc)


@app.command()
def doctor(
    project_dir: Path = typer.Option(  # noqa: B008
        Path.cwd(), "--directory", "-d", help="Project directory (default: cwd)."
    ),
) -> None:
    """Health check: podman, GPU, image pullable, manifest valid."""
    pass_count = 0
    fail_count = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal pass_count, fail_count
        marker = typer.style("✓", fg=typer.colors.GREEN) if ok else typer.style(
            "✗", fg=typer.colors.RED
        )
        line = f"  {marker} {label}"
        if detail:
            line += f"  ({detail})"
        typer.echo(line)
        if ok:
            pass_count += 1
        else:
            fail_count += 1

    typer.echo("=== environment ===")
    check("podman on PATH", has_podman(), detail="from $PATH")
    gpu = detect_gpu()
    check(f"GPU detected: {gpu}", gpu != "none")

    typer.echo("\n=== manifest ===")
    manifest_path = project_dir / "omnilab.yaml"
    if not manifest_path.exists():
        check(
            "omnilab.yaml present",
            ok=False,
            detail=f"none at {manifest_path} — try `omnilab new`",
        )
    else:
        try:
            m = OmnilabManifest.from_yaml(manifest_path)
            check(f"omnilab.yaml parses (project={m.name})", ok=True)

            typer.echo("\n=== image ===")
            if has_podman():
                # `podman manifest inspect` fetches metadata without pulling
                # layers — quick check that the image ref resolves.
                rc = run(["podman", "manifest", "inspect", m.image])
                ok = rc.returncode == 0
                detail = "manifest fetched" if ok else (rc.stderr.strip().splitlines() or [""])[0]
                check(f"image '{m.image}' pullable", ok=ok, detail=detail)
            else:
                check("image reachability", ok=False, detail="skipped (no podman)")
        except Exception as e:  # noqa: BLE001
            check("omnilab.yaml parses", ok=False, detail=str(e))

    typer.echo(f"\nResult: {pass_count} passed, {fail_count} failed.")
    raise typer.Exit(fail_count)
