"""Confirmation + dry-run helpers for destructive commands.

Per project-spec-v1.md (rev 3) § "CLI conventions": every destructive
command supports `--dry-run` (preview only) and `--yes` (skip prompts);
default behavior previews and asks. This module centralizes the pattern
so individual commands stay short.
"""

from __future__ import annotations

from typing import Any

import typer

from . import _output


def confirm_or_exit(
    *,
    summary: str,
    items: list[str] | None = None,
    yes: bool = False,
    dry_run: bool = False,
    abort_code: int = 0,
    json_payload: dict[str, Any] | None = None,
) -> None:
    """Standard confirmation flow for destructive commands.

    Parameters
    ----------
    summary
        One-line description of what will happen.
    items
        Specific things being acted on (containers, processes, files).
        Each rendered on its own bullet line in human mode.
    yes
        If True, skip the prompt entirely (scripting / CI).
    dry_run
        If True, print the preview and exit cleanly with `abort_code`
        without taking any action.
    abort_code
        Exit code used for both `--dry-run` and a "no" answer to the
        confirmation. Default 0 — abort is a deliberate user choice,
        not an error.
    json_payload
        If we're in JSON mode, the structured preview to emit (e.g.
        the planned cleanup actions). When None, a default summary
        with `items` is built.
    """
    if _output.is_json_mode():
        payload = dict(json_payload or {})
        payload.setdefault("summary", summary)
        payload.setdefault("items", items or [])
        payload["dry_run"] = dry_run
        if dry_run:
            payload["aborted"] = True
            _output.emit(data=payload)
            raise typer.Exit(abort_code)
        if yes:
            _output.emit(data=payload)
            return
        # In JSON mode we don't prompt interactively; require --yes
        # explicitly. Anything else is a state error.
        payload["error"] = "destructive action requires --yes in --json mode"
        _output.emit(data=payload)
        raise typer.Exit(2)

    # --- human mode ---
    typer.echo(summary)
    if items:
        for item in items:
            typer.echo(f"  - {item}")

    if dry_run:
        typer.echo("\nDry-run: no changes made.")
        raise typer.Exit(abort_code)

    if yes:
        return

    if not typer.confirm("\nProceed?", default=False):
        typer.echo("Aborted.")
        raise typer.Exit(abort_code)
