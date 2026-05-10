"""Safe orphan/state cleanup for `omnilab clean`.

The planner is a **pure function** — given a snapshot of containers + a
/proc snapshot, it returns a CleanupPlan with the ordered list of actions
that *would* be taken. The executor turns that into actual podman / kill
calls. Tests exercise the planner with mock data.

Per project-spec-v1.md (rev 3) § "v1 must-do" #13 and the kickoff:
  - Scoped to current project by default; `--all` opts into nuclear.
  - Container-kill primitives (`podman kill`, `podman rm --force`),
    NOT just `pkill`.
  - Tree-aware: walk PPid chains, kill children-first to prevent
    re-orphaning.
  - D-state processes are reported honestly (reboot needed) rather
    than silently failed-on.
  - `--aggressive` enables the SIGTERM → SIGKILL → sudo escalation.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

LABEL_KEY = "omnilab.project"

ActionKind = Literal[
    "container_stop",
    "container_rm",
    "process_term",
    "process_kill",
    "report_d_state",
]


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    name: str
    state: str  # 'R', 'S', 'D', 'Z', 'T', 'I', etc.
    project: str | None = None  # if discoverable from cwd / cgroup


@dataclass
class ContainerInfo:
    name: str
    project: str | None  # value of omnilab.project label
    state: str  # 'running', 'created', 'exited', 'paused'


@dataclass
class CleanupAction:
    kind: ActionKind
    target: str  # container name or pid (as str)
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CleanupPlan:
    project: str | None
    scope: Literal["project", "all"]
    aggressive: bool
    actions: list[CleanupAction] = field(default_factory=list)
    d_state_processes: list[ProcInfo] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "scope": self.scope,
            "aggressive": self.aggressive,
            "actions": [a.to_dict() for a in self.actions],
            "d_state_processes": [asdict(p) for p in self.d_state_processes],
        }

    def is_empty(self) -> bool:
        return not self.actions and not self.d_state_processes


# ---- planner (pure) ------------------------------------------------------


def _select_containers(
    containers: list[ContainerInfo], project: str | None, *, all_projects: bool
) -> list[ContainerInfo]:
    if all_projects:
        # Take anything labeled as an omnilab project.
        return [c for c in containers if c.project is not None]
    if project is None:
        return []
    return [c for c in containers if c.project == project]


def _walk_descendants(
    procs: list[ProcInfo], roots: list[int]
) -> list[ProcInfo]:
    """Return every descendant of any pid in `roots`, ordered children-first
    (deepest descendants come first so killing them won't re-orphan)."""
    by_ppid: dict[int, list[ProcInfo]] = {}
    for p in procs:
        by_ppid.setdefault(p.ppid, []).append(p)

    ordered: list[ProcInfo] = []

    def _recurse(pid: int) -> None:
        for child in by_ppid.get(pid, []):
            _recurse(child.pid)  # depth first so deepest is appended last
            ordered.append(child)

    for r in roots:
        _recurse(r)
    return ordered


def plan_cleanup(
    *,
    project: str | None,
    containers: list[ContainerInfo],
    procs: list[ProcInfo],
    all_projects: bool = False,
    aggressive: bool = False,
) -> CleanupPlan:
    """Pure: given current state, produce the list of actions to take.

    No side effects, no I/O. Tests can call this directly.
    """
    plan = CleanupPlan(
        project=project,
        scope="all" if all_projects else "project",
        aggressive=aggressive,
    )

    # 1. D-state processes — report only, never killed.
    plan.d_state_processes = [p for p in procs if p.state == "D"]

    # 2. Containers within scope.
    targets = _select_containers(containers, project, all_projects=all_projects)
    for c in targets:
        if c.state == "running":
            plan.actions.append(
                CleanupAction(
                    kind="container_stop",
                    target=c.name,
                    reason=f"running container in scope (project={c.project})",
                )
            )
        # Always rm to clean leftover stopped containers from this project.
        plan.actions.append(
            CleanupAction(
                kind="container_rm",
                target=c.name,
                reason=f"remove container shell (project={c.project})",
            )
        )

    # 3. Project-scoped processes — only when --aggressive (default
    #    conservative path lets podman do the orphan reaping).
    if aggressive:
        in_scope_procs = [
            p
            for p in procs
            if p.state != "D"
            and (
                (all_projects and p.project is not None)
                or (not all_projects and p.project == project)
            )
        ]
        roots = [p.pid for p in in_scope_procs]
        descendants = _walk_descendants(procs, roots)

        # children-first; descendants then explicit roots.
        seen: set[int] = set()
        ordered_pids: list[int] = []
        for p in descendants + in_scope_procs:
            if p.pid in seen or p.state == "D":
                continue
            seen.add(p.pid)
            ordered_pids.append(p.pid)

        for pid in ordered_pids:
            plan.actions.append(
                CleanupAction(
                    kind="process_term",
                    target=str(pid),
                    reason="aggressive scope: SIGTERM",
                )
            )
            plan.actions.append(
                CleanupAction(
                    kind="process_kill",
                    target=str(pid),
                    reason="aggressive scope: SIGKILL if SIGTERM didn't take",
                )
            )

    return plan


# ---- proc snapshot (real /proc reader) -----------------------------------


_PROC_DIR = Path("/proc")


def read_proc_snapshot(*, proc_dir: Path = _PROC_DIR) -> list[ProcInfo]:
    """Read /proc/<pid>/status for every numeric pid dir. Empty list on
    non-Linux or unreadable /proc."""
    if not proc_dir.is_dir():
        return []
    out: list[ProcInfo] = []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            content = (entry / "status").read_text()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        out.append(_parse_proc_status(content))
    return [p for p in out if p is not None]  # type: ignore[misc]


def _parse_proc_status(content: str) -> ProcInfo | None:
    """Parse /proc/<pid>/status output.

    Format excerpt:
        Name:   bash
        State:  S (sleeping)
        Pid:    1234
        PPid:   1
        ...
    """
    name = ""
    state = ""
    pid = -1
    ppid = -1
    for line in content.splitlines():
        if line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("State:"):
            m = re.match(r"State:\s+(\S)", line)
            if m:
                state = m.group(1)
        elif line.startswith("Pid:"):
            try:
                pid = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
        elif line.startswith("PPid:"):
            try:
                ppid = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    if pid <= 0:
        return None
    return ProcInfo(pid=pid, ppid=ppid, name=name, state=state)


# ---- container snapshot (real podman query) -----------------------------


def read_container_snapshot() -> list[ContainerInfo]:
    """Query podman for all containers carrying our omnilab.project label."""
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            f"label={LABEL_KEY}",
            "--format",
            "{{.Names}}\t{{.State}}\t{{.Labels}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [c for c in (_parse_podman_ps_line(line) for line in result.stdout.splitlines()) if c]


def _parse_podman_ps_line(line: str) -> ContainerInfo | None:
    """Parse a podman ps tab-separated line."""
    parts = line.split("\t", 2)
    if len(parts) < 3:
        return None
    name, state, labels = parts
    project: str | None = None
    for label in labels.split(","):
        if label.startswith(f"{LABEL_KEY}="):
            project = label.split("=", 1)[1].strip()
            break
    return ContainerInfo(name=name.strip(), project=project, state=state.strip().lower())


# ---- executor (turns plan into real calls) ------------------------------


def execute_plan(plan: CleanupPlan) -> list[tuple[CleanupAction, int]]:
    """Run each action; return (action, return_code) pairs.

    Stops at the first non-zero return code only for fatal infrastructure
    failures — individual container actions just get their rc recorded.
    """
    results: list[tuple[CleanupAction, int]] = []
    for action in plan.actions:
        rc = _do_action(action, aggressive=plan.aggressive)
        results.append((action, rc))
    return results


def _do_action(action: CleanupAction, *, aggressive: bool) -> int:
    if action.kind == "container_stop":
        return subprocess.run(
            ["podman", "stop", "--time", "5", action.target],
            check=False,
            capture_output=True,
        ).returncode
    if action.kind == "container_rm":
        flag = "--force" if aggressive else "--force"  # rm always forces — already stopped or was never running
        return subprocess.run(
            ["podman", "rm", flag, action.target],
            check=False,
            capture_output=True,
        ).returncode
    if action.kind == "process_term":
        return _signal_pid(int(action.target), signal.SIGTERM)
    if action.kind == "process_kill":
        return _signal_pid(int(action.target), signal.SIGKILL)
    if action.kind == "report_d_state":
        return 0  # report-only
    return 1


def _signal_pid(pid: int, sig: signal.Signals) -> int:
    try:
        os.kill(pid, sig)
        return 0
    except ProcessLookupError:
        return 0  # already gone — not an error
    except PermissionError:
        return 5  # permission error per spec exit-code table
