"""Textual TUI for `omnilab gpu`. Lazy-imported so test runs without
`textual` installed still work — same pattern as `inspect_tui.py`.

The UI is deliberately a thin shell over `gpu_doctor`: it renders the check
ladder, offers one button to wake the GPU and one to apply every auto-fix,
and re-probes after each. All decision logic lives in `gpu_doctor.evaluate`
so it stays testable without a terminal.
"""

from __future__ import annotations

from .gpu_doctor import (
    Check,
    GpuProbe,
    apply_fix,
    autofixable,
    evaluate,
    manual_actions,
    probe_host,
    summarize,
)

_ICON = {"ok": "✓", "warn": "!", "fail": "✗"}


def run_tui(*, initial: GpuProbe | None = None) -> int:
    """Launch the GPU TUI. Returns the exit code typer should use."""
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Horizontal, VerticalScroll
        from textual.widgets import Button, Footer, Header, Static
    except ImportError as e:  # pragma: no cover — handled at call time only
        raise RuntimeError(
            "textual is not installed. Install the [tui] extra: "
            "`pip install -e .[tui]` or `pip install textual`."
        ) from e

    class GpuApp(App):
        TITLE = "omnilab gpu — NVIDIA diagnosis & repair"
        CSS = """
        #summary { padding: 0 1; height: auto; }
        #checks  { border: round $accent; padding: 0 1; height: 1fr; }
        #actions { height: auto; padding: 1 1; }
        #log     { border: round $primary; padding: 0 1; height: 8; }
        Button   { margin: 0 1; }
        """
        BINDINGS = [
            ("r", "reprobe", "Re-probe"),
            ("w", "wake", "Wake GPU"),
            ("f", "fixall", "Fix all"),
            ("q", "quit", "Quit"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.probe: GpuProbe = initial if initial is not None else GpuProbe()
            self.checks: list[Check] = []
            self._log: list[str] = []

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static("probing…", id="summary")
            yield VerticalScroll(Static("", id="checks-body"), id="checks")
            with Horizontal(id="actions"):
                yield Button("Wake GPU", id="wake", variant="primary")
                yield Button("Apply all fixes", id="fixall", variant="warning")
                yield Button("Re-probe", id="reprobe")
            yield Static("", id="log")
            yield Footer()

        def on_mount(self) -> None:
            if initial is None:
                self.action_reprobe()
            else:
                self._refresh_view()

        # ---- actions ----

        def action_reprobe(self) -> None:
            self._note("probing host…")
            self.probe = probe_host(wake=False)
            self._refresh_view()

        def action_wake(self) -> None:
            """Querying the GPU is what resumes it from D3cold."""
            self._note("waking GPU (nvidia-smi)…")
            self.probe = probe_host(wake=True)
            self._refresh_view()
            state = self.probe.runtime_status or "unknown"
            self._note(f"power state now: {state}")

        def action_fixall(self) -> None:
            fixes = autofixable(self.checks)
            if not fixes:
                self._note("nothing auto-fixable")
                return
            for check in fixes:
                assert check.fix is not None
                self._note(f"→ {check.fix.description}")
                ok, lines = apply_fix(check.fix)
                for line in lines:
                    self._note(f"   {line}")
                if not ok:
                    self._note(f"   stopped at {check.key}")
                    break
            self.action_reprobe()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            {"wake": self.action_wake, "fixall": self.action_fixall, "reprobe": self.action_reprobe}[
                str(event.button.id)
            ]()

        # ---- rendering ----

        def _refresh_view(self) -> None:
            self.checks = evaluate(self.probe)
            s = summarize(self.checks)
            c = s["counts"]
            self.query_one("#summary", Static).update(
                f"overall: {s['overall'].upper()}   "
                f"{c['ok']} ok · {c['warn']} warn · {c['fail']} fail   "
                f"({len(autofixable(self.checks))} auto-fixable)"
            )
            self.query_one("#checks-body", Static).update(format_checks(self.checks))

        def _note(self, line: str) -> None:
            self._log.append(line)
            self.query_one("#log", Static).update("\n".join(self._log[-6:]))

    GpuApp().run()
    return 0


# ---- pure formatters (testable without a terminal) ---------------------


def format_checks(checks: list[Check]) -> str:
    """Render the ladder as text. Used by the TUI and by `--no-tui`."""
    if not checks:
        return "(no checks run)"
    lines: list[str] = []
    for c in checks:
        lines.append(f"{_ICON[c.severity]} {c.title}")
        if c.detail:
            lines.append(f"    {c.detail}")
        if c.fix and not c.ok:
            if c.fix.auto and c.fix.argv:
                lines.append(f"    fix: {c.fix.description}")
            elif c.fix.manual_hint:
                for hint in c.fix.manual_hint.splitlines():
                    lines.append(f"    {hint}")
    return "\n".join(lines)


def format_report(checks: list[Check]) -> str:
    """Full human report: ladder, then a manual-action appendix."""
    out = [format_checks(checks)]
    manual = manual_actions(checks)
    auto = autofixable(checks)
    if auto:
        out.append("")
        out.append(f"{len(auto)} issue(s) can be fixed automatically — run `omnilab gpu --fix`.")
    if manual:
        out.append("")
        out.append("Needs you (cannot be fixed from the CLI):")
        for c in manual:
            out.append(f"  - {c.title}: {c.detail}")
    return "\n".join(out)
