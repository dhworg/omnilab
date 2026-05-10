"""Dual-mode output helpers — every read-only command emits both human
text/TUI and a `--json` snapshot. Mode is selected once by the root
typer callback before any command runs.

Per project-spec-v1.md (rev 3) § "CLI conventions".
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer

# Module-level mode flag, set by the root callback in cli.py.
_json_mode: bool = False


def set_json_mode(enabled: bool) -> None:
    """Set the active output mode. Called once by the root callback."""
    global _json_mode  # noqa: PLW0603
    _json_mode = enabled


def is_json_mode() -> bool:
    return _json_mode


def emit(human: str | None = None, *, data: Any = None) -> None:
    """Emit output in the active mode.

    - JSON mode: writes `data` (or {} if None) to stdout as compact JSON.
    - Human mode: writes `human` text via typer.echo. Silent if `human`
      is None.
    """
    if _json_mode:
        payload = data if data is not None else {}
        json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
        sys.stdout.write("\n")
    elif human is not None:
        typer.echo(human)


def emit_error(message: str, *, code: int = 1, **extra: Any) -> None:
    """Emit an error and raise typer.Exit(code).

    Exit codes per spec § "Documented exit codes":
      0 = success
      1 = generic error
      2 = invalid args
      3 = state error (e.g. container not running)
      4 = network/auth error
      5 = permission error
    """
    if _json_mode:
        payload = {"error": message, "code": code, **extra}
        json.dump(payload, sys.stderr, default=str)
        sys.stderr.write("\n")
    else:
        typer.echo(f"ERROR: {message}", err=True)
    raise typer.Exit(code)


def style_pass() -> str:
    return typer.style("✓", fg=typer.colors.GREEN) if not _json_mode else "PASS"


def style_fail() -> str:
    return typer.style("✗", fg=typer.colors.RED) if not _json_mode else "FAIL"
