"""Textual-based TUI for `omnilab inspect`. Lazy-imported so test runs
without `textual` available still work."""

from __future__ import annotations

from .inspect import InspectSnapshot, build_snapshot
from .inspect_sources import Sources


def run_tui(sources: Sources, *, container: str, refresh_hz: float = 1.0) -> int:
    """Launch the TUI. Imports textual lazily.

    Returns the exit code typer should use.
    """
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Grid
        from textual.widgets import Footer, Header, Static
    except ImportError as e:  # pragma: no cover — handled at call time only
        raise RuntimeError(
            "textual is not installed. Install the [tui] extra: "
            "`pip install -e .[tui]` or `pip install textual`."
        ) from e

    refresh_interval = max(1.0 / refresh_hz, 0.1)

    class InspectApp(App):
        TITLE = f"omnilab inspect — {container}"
        CSS = """
        Grid#main { grid-size: 2 3; grid-gutter: 1; }
        Static.section { border: round $accent; padding: 0 1; }
        DataTable { height: auto; }
        """

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Grid(id="main"):
                yield self._panel("nodes")
                yield self._panel("topics")
                yield self._panel("services")
                yield self._panel("tf_frames")
                yield self._panel("gazebo")
            yield Footer()

        def _panel(self, key: str) -> Static:
            s = Static("loading…", classes="section", id=f"panel-{key}")
            s.border_title = key
            return s

        def on_mount(self) -> None:
            self.refresh_data()
            self.set_interval(refresh_interval, self.refresh_data)

        def refresh_data(self) -> None:
            snap: InspectSnapshot = build_snapshot(sources, container=container)
            self._render_panel("nodes", _format_nodes(snap))
            self._render_panel("topics", _format_topics(snap))
            self._render_panel("services", _format_services(snap))
            self._render_panel("tf_frames", _format_tf(snap))
            self._render_panel("gazebo", _format_gazebo(snap))

        def _render_panel(self, key: str, text: str) -> None:
            panel = self.query_one(f"#panel-{key}", Static)
            panel.update(text)

    app = InspectApp()
    app.run()
    return 0


# ---- panel formatters (pure; testable independently) -------------------


def _format_nodes(snap: InspectSnapshot) -> str:
    if not snap.nodes:
        return "(no nodes)"
    lines = []
    for n in snap.nodes[:30]:  # cap to keep panel sized
        cpu = f"{n.cpu_percent:.1f}%" if n.cpu_percent is not None else "—"
        mem = f"{n.mem_mb:.0f}MB" if n.mem_mb is not None else "—"
        warn = "⚠ " if n.warnings else "  "
        lines.append(f"{warn}{n.name}  {cpu} / {mem}")
    if len(snap.nodes) > 30:
        lines.append(f"… and {len(snap.nodes) - 30} more")
    return "\n".join(lines)


def _format_topics(snap: InspectSnapshot) -> str:
    if not snap.topics:
        return "(no topics)"
    lines = []
    for t in snap.topics[:30]:
        rate = f"{t.rate_hz:.1f}Hz" if t.rate_hz is not None else "—"
        bw = (
            f"{t.bandwidth_bytes_per_sec / 1024:.1f}KB/s"
            if t.bandwidth_bytes_per_sec is not None
            else "—"
        )
        lines.append(f"{t.name}  {rate}  {bw}")
    if len(snap.topics) > 30:
        lines.append(f"… and {len(snap.topics) - 30} more")
    return "\n".join(lines)


def _format_services(snap: InspectSnapshot) -> str:
    if not snap.services:
        return "(no services)"
    return "\n".join(f"{s.name}  [{s.type}]" for s in snap.services[:30])


def _format_tf(snap: InspectSnapshot) -> str:
    if not snap.tf_frames:
        return "(no TF frames seen)"
    lines = []
    for f in snap.tf_frames[:20]:
        flag = ""
        if f.stale:
            flag = " ⚠STALE"
        elif f.missing:
            flag = " ⚠MISSING"
        parent = f.parent or "(root)"
        lines.append(f"{f.name}  ← {parent}{flag}")
    return "\n".join(lines)


def _format_gazebo(snap: InspectSnapshot) -> str:
    g = snap.gazebo
    state = "✓ connected" if g.connected else "✗ no Gazebo"
    sim = f"sim_time={g.sim_time:.2f}s" if g.sim_time is not None else "sim_time=—"
    rtf = f"rtf={g.rtf:.2f}" if g.rtf is not None else "rtf=—"
    return f"{state}\n{sim}\n{rtf}"
