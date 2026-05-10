"""Tests for omnilab.clean — planner is pure; mock /proc + podman."""

from __future__ import annotations

import textwrap

from omnilab.clean import (
    ContainerInfo,
    ProcInfo,
    _parse_podman_ps_line,
    _parse_proc_status,
    _walk_descendants,
    plan_cleanup,
)

# ---- /proc parser -------------------------------------------------------


def test_parse_proc_status_basic():
    p = _parse_proc_status(
        textwrap.dedent(
            """\
            Name:   bash
            State:  S (sleeping)
            Pid:    1234
            PPid:   1
            """
        )
    )
    assert p is not None
    assert p.name == "bash"
    assert p.state == "S"
    assert p.pid == 1234
    assert p.ppid == 1


def test_parse_proc_status_d_state():
    p = _parse_proc_status("Name: x\nState: D (disk sleep)\nPid: 5\nPPid: 1\n")
    assert p is not None
    assert p.state == "D"


def test_parse_proc_status_zombie():
    p = _parse_proc_status("Name: z\nState: Z (zombie)\nPid: 6\nPPid: 1\n")
    assert p is not None
    assert p.state == "Z"


def test_parse_proc_status_invalid():
    assert _parse_proc_status("garbage") is None


# ---- podman ps parser ---------------------------------------------------


def test_parse_podman_ps_line():
    line = "myproj\trunning\tomnilab.project=myproj,foo=bar"
    c = _parse_podman_ps_line(line)
    assert c is not None
    assert c.name == "myproj"
    assert c.state == "running"
    assert c.project == "myproj"


def test_parse_podman_ps_line_no_label():
    line = "stranger\trunning\tfoo=bar"
    c = _parse_podman_ps_line(line)
    assert c is not None
    assert c.project is None


def test_parse_podman_ps_line_invalid():
    assert _parse_podman_ps_line("not enough fields") is None


# ---- descendant walker --------------------------------------------------


def test_walk_descendants_children_first():
    procs = [
        ProcInfo(pid=10, ppid=1, name="parent", state="S"),
        ProcInfo(pid=20, ppid=10, name="child", state="S"),
        ProcInfo(pid=30, ppid=20, name="grandchild", state="S"),
    ]
    desc = _walk_descendants(procs, roots=[10])
    pids = [p.pid for p in desc]
    # Deepest first: 30 before 20.
    assert pids == [30, 20]


def test_walk_descendants_no_children():
    procs = [ProcInfo(pid=99, ppid=1, name="x", state="S")]
    assert _walk_descendants(procs, roots=[99]) == []


# ---- planner: scope rules -----------------------------------------------


def test_plan_default_scope_only_current_project():
    containers = [
        ContainerInfo(name="my", project="my", state="running"),
        ContainerInfo(name="other", project="other", state="running"),
    ]
    plan = plan_cleanup(project="my", containers=containers, procs=[])
    targets = {a.target for a in plan.actions}
    assert "other" not in targets
    assert "my" in targets


def test_plan_all_projects_takes_everything():
    containers = [
        ContainerInfo(name="my", project="my", state="running"),
        ContainerInfo(name="other", project="other", state="running"),
        ContainerInfo(name="stranger", project=None, state="running"),
    ]
    plan = plan_cleanup(
        project=None, containers=containers, procs=[], all_projects=True
    )
    targets = {a.target for a in plan.actions}
    assert "my" in targets
    assert "other" in targets
    assert "stranger" not in targets  # no omnilab.project label


def test_plan_running_container_gets_stop_then_rm():
    plan = plan_cleanup(
        project="p",
        containers=[ContainerInfo(name="p", project="p", state="running")],
        procs=[],
    )
    kinds = [a.kind for a in plan.actions]
    assert kinds == ["container_stop", "container_rm"]


def test_plan_stopped_container_gets_only_rm():
    plan = plan_cleanup(
        project="p",
        containers=[ContainerInfo(name="p", project="p", state="exited")],
        procs=[],
    )
    kinds = [a.kind for a in plan.actions]
    assert kinds == ["container_rm"]


# ---- planner: D-state honest reporting ----------------------------------


def test_plan_d_state_is_reported_not_killed():
    procs = [
        ProcInfo(pid=10, ppid=1, name="stuck", state="D", project="p"),
        ProcInfo(pid=11, ppid=1, name="alive", state="S", project="p"),
    ]
    plan = plan_cleanup(
        project="p",
        containers=[],
        procs=procs,
        aggressive=True,
    )
    pids_acted_on = [a.target for a in plan.actions if a.kind in {"process_term", "process_kill"}]
    assert "10" not in pids_acted_on  # D-state never gets a kill action
    assert "11" in pids_acted_on
    assert any(p.pid == 10 for p in plan.d_state_processes)


def test_plan_d_state_report_even_when_not_aggressive():
    procs = [ProcInfo(pid=10, ppid=1, name="stuck", state="D")]
    plan = plan_cleanup(project="p", containers=[], procs=procs)
    assert any(p.pid == 10 for p in plan.d_state_processes)
    # Default scope has no aggressive process actions.
    assert not any(a.kind in {"process_term", "process_kill"} for a in plan.actions)


# ---- planner: aggressive walks tree children-first ----------------------


def test_plan_aggressive_kills_children_before_parents():
    procs = [
        ProcInfo(pid=10, ppid=1, name="parent", state="S", project="p"),
        ProcInfo(pid=20, ppid=10, name="child", state="S"),
        ProcInfo(pid=30, ppid=20, name="grandchild", state="S"),
    ]
    plan = plan_cleanup(
        project="p", containers=[], procs=procs, aggressive=True
    )
    term_pids = [a.target for a in plan.actions if a.kind == "process_term"]
    # Deepest first: 30 → 20 → 10.
    assert term_pids.index("30") < term_pids.index("20") < term_pids.index("10")


def test_plan_non_aggressive_skips_processes():
    procs = [ProcInfo(pid=10, ppid=1, name="x", state="S", project="p")]
    plan = plan_cleanup(project="p", containers=[], procs=procs)
    assert not any(a.kind in {"process_term", "process_kill"} for a in plan.actions)


# ---- empty plan / dict serialization ------------------------------------


def test_empty_plan_is_empty():
    plan = plan_cleanup(project="x", containers=[], procs=[])
    assert plan.is_empty()


def test_plan_to_dict_round_trips():
    import json

    plan = plan_cleanup(
        project="p",
        containers=[ContainerInfo(name="p", project="p", state="running")],
        procs=[ProcInfo(pid=99, ppid=1, name="z", state="D")],
    )
    serialized = json.dumps(plan.to_dict())
    parsed = json.loads(serialized)
    assert parsed["scope"] == "project"
    assert parsed["project"] == "p"
    assert len(parsed["actions"]) == 2  # stop + rm
    assert len(parsed["d_state_processes"]) == 1
