"""Tests for omnilab.gpu_doctor — the check ladder, fix planning, summaries.

`evaluate()` is pure over a GpuProbe, so every failure mode below is
reproducible without an NVIDIA GPU (or a Linux host) present.
"""

from __future__ import annotations

import pytest

from omnilab.gpu_doctor import (
    PRIME_ENV,
    REQUIRED_MODULES,
    Fix,
    GpuProbe,
    apply_fix,
    autofixable,
    classify_nvrm,
    evaluate,
    fix_argv,
    manual_actions,
    summarize,
)

ALL_MODULES = frozenset(REQUIRED_MODULES)
ALL_NODES = frozenset(
    {"/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-modeset", "/dev/nvidia0"}
)


def _healthy(**overrides) -> GpuProbe:
    """A probe where everything on the host side is correct."""
    base = {
        "pci_present": True,
        "pci_address": "0000:01:00.0",
        "pci_name": "NVIDIA GeForce GTX 1050",
        "runtime_status": "active",
        "modules_loaded": ALL_MODULES,
        "drm_modeset": True,
        "device_nodes": ALL_NODES,
        "nvidia_smi_ok": True,
        "driver_version": "580.65.06",
        "toolkit_present": True,
        "cdi_spec_present": True,
        "cdi_driver_version": "580.65.06",
        "secure_boot": False,
    }
    base.update(overrides)
    return GpuProbe(**base)


def _by_key(checks) -> dict:
    return {c.key: c for c in checks}


# ---- happy path ---------------------------------------------------------


def test_healthy_host_passes_everything():
    checks = evaluate(_healthy())
    assert summarize(checks)["overall"] == "ok"
    assert autofixable(checks) == []
    assert manual_actions(checks) == []


def test_summarize_counts_add_up():
    checks = evaluate(_healthy(runtime_status="suspended", toolkit_present=False))
    s = summarize(checks)
    assert s["counts"]["ok"] + s["counts"]["warn"] + s["counts"]["fail"] == s["total"]
    assert s["total"] == len(checks)


# ---- no GPU short-circuits ---------------------------------------------


def test_no_pci_device_short_circuits_ladder():
    """Nothing downstream is meaningful without a GPU on the bus."""
    checks = evaluate(GpuProbe(pci_present=False))
    assert len(checks) == 1
    assert checks[0].key == "pci_present"
    assert checks[0].severity == "fail"
    # Firmware/MUX is not something we can fix from userspace.
    assert checks[0].fix is not None and not checks[0].fix.auto


# ---- the D3cold case ----------------------------------------------------


def test_suspended_gpu_is_warn_not_fail_and_offers_wake():
    """A suspended dGPU is the power-saving default, not a fault."""
    checks = _by_key(evaluate(_healthy(runtime_status="suspended")))
    power = checks["power_state"]
    assert power.severity == "warn"
    assert power.fix is not None and power.fix.auto
    assert power.fix.argv == [["nvidia-smi", "-L"]]
    assert not power.fix.needs_root


def test_unknown_runtime_status_warns_without_a_fix():
    checks = _by_key(evaluate(_healthy(runtime_status="")))
    assert checks["power_state"].severity == "warn"
    assert checks["power_state"].fix is None


# ---- modules ------------------------------------------------------------


def test_missing_nvidia_uvm_fails_with_modprobe_fix():
    """nvidia_uvm loads lazily — a host can look fine and still fail CUDA."""
    probe = _healthy(modules_loaded=ALL_MODULES - {"nvidia_uvm"})
    mod = _by_key(evaluate(probe))["modules"]
    assert mod.severity == "fail"
    assert "nvidia_uvm" in mod.detail
    assert mod.fix is not None and mod.fix.auto and mod.fix.needs_root
    assert mod.fix.argv == [["modprobe", "nvidia_uvm"]]


def test_secure_boot_with_missing_modules_is_manual_not_modprobe():
    """modprobe cannot win against Secure Boot rejecting unsigned modules."""
    probe = _healthy(modules_loaded=frozenset(), secure_boot=True)
    mod = _by_key(evaluate(probe))["modules"]
    assert mod.severity == "fail"
    assert mod.fix is not None and not mod.fix.auto
    assert "mokutil" in mod.fix.manual_hint
    assert mod not in autofixable(evaluate(probe))


def test_secure_boot_enabled_but_modules_loaded_is_fine():
    checks = _by_key(evaluate(_healthy(secure_boot=True)))
    assert checks["modules"].severity == "ok"


# ---- device nodes -------------------------------------------------------


def test_missing_device_nodes_autofix_does_not_need_root():
    """nvidia-modprobe is setuid root by design."""
    probe = _healthy(device_nodes=frozenset({"/dev/nvidiactl"}))
    node = _by_key(evaluate(probe))["device_nodes"]
    assert node.severity == "fail"
    assert node.fix is not None and node.fix.auto
    assert not node.fix.needs_root
    assert node.fix.argv == [["nvidia-modprobe", "-c", "0", "-u"]]


def test_control_nodes_present_but_no_gpu_node_still_fails():
    probe = _healthy(
        device_nodes=frozenset({"/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-modeset"})
    )
    node = _by_key(evaluate(probe))["device_nodes"]
    assert node.severity == "fail"
    assert "nvidiaN" in node.detail


def test_multi_gpu_nodes_pass():
    probe = _healthy(device_nodes=ALL_NODES | {"/dev/nvidia1", "/dev/nvidia2"})
    assert _by_key(evaluate(probe))["device_nodes"].severity == "ok"


# ---- modesetting --------------------------------------------------------


def test_modeset_off_warns_and_is_manual():
    checks = _by_key(evaluate(_healthy(drm_modeset=False)))
    m = checks["drm_modeset"]
    assert m.severity == "warn"
    assert m.fix is not None and not m.fix.auto
    assert "nvidia-drm.modeset=1" in m.fix.manual_hint


def test_modeset_unknown_emits_no_check():
    """None means we couldn't read it — don't invent a verdict."""
    assert "drm_modeset" not in _by_key(evaluate(_healthy(drm_modeset=None)))


# ---- CDI: the most common container-GPU failure -------------------------


def test_missing_cdi_spec_fails_with_generate_fix():
    probe = _healthy(cdi_spec_present=False, cdi_driver_version="")
    cdi = _by_key(evaluate(probe))["cdi_spec"]
    assert cdi.severity == "fail"
    assert cdi.fix is not None and cdi.fix.auto and cdi.fix.needs_root
    assert cdi.fix.argv[0][:3] == ["nvidia-ctk", "cdi", "generate"]


def test_stale_cdi_spec_after_driver_update_is_detected():
    """The classic: driver upgraded, CDI spec still names the old version."""
    probe = _healthy(driver_version="580.65.06", cdi_driver_version="550.120")
    cdi = _by_key(evaluate(probe))["cdi_spec"]
    assert cdi.severity == "fail"
    assert "stale" in cdi.detail
    assert "550.120" in cdi.detail and "580.65.06" in cdi.detail


def test_cdi_not_checked_when_toolkit_absent():
    """No toolkit means no CDI concept — don't stack a confusing second failure."""
    checks = _by_key(evaluate(_healthy(toolkit_present=False)))
    assert checks["toolkit"].severity == "fail"
    assert "cdi_spec" not in checks


def test_cdi_version_unknown_does_not_false_fail():
    probe = _healthy(cdi_driver_version="")
    assert _by_key(evaluate(probe))["cdi_spec"].severity == "ok"


# ---- the bug this module exists for -------------------------------------


def test_llvmpipe_renderer_is_the_driver_works_gpu_unused_case():
    probe = _healthy(container_smi_ok=True, container_renderer="llvmpipe (LLVM 17.0.6, 256 bits)")
    r = _by_key(evaluate(probe))["render_offload"]
    assert r.severity == "fail"
    assert "CPU" in r.detail
    for key in PRIME_ENV:
        assert key in r.fix.manual_hint


def test_nvidia_renderer_passes():
    probe = _healthy(container_smi_ok=True, container_renderer="NVIDIA GeForce GTX 1050/PCIe/SSE2")
    assert _by_key(evaluate(probe))["render_offload"].severity == "ok"


def test_unrecognised_renderer_warns_rather_than_fails():
    probe = _healthy(container_smi_ok=True, container_renderer="Mesa Intel(R) UHD Graphics")
    assert _by_key(evaluate(probe))["render_offload"].severity == "warn"


def test_container_stage_skipped_when_not_probed():
    checks = _by_key(evaluate(_healthy()))
    assert "container_gpu" not in checks
    assert "render_offload" not in checks


def test_container_smi_failure_suggests_cdi_regen():
    probe = _healthy(container_smi_ok=False)
    c = _by_key(evaluate(probe))["container_gpu"]
    assert c.severity == "fail"
    assert c.fix is not None and c.fix.auto


# ---- vendor daemons -----------------------------------------------------


def test_vendor_daemon_is_named_not_fought():
    probe = _healthy(vendor_daemons=frozenset({"system76-power"}))
    v = _by_key(evaluate(probe))["vendor_daemon"]
    assert v.severity == "warn"
    assert v.fix is not None and not v.fix.auto
    assert "system76-power graphics nvidia" in v.fix.manual_hint


def test_no_vendor_daemon_emits_no_check():
    assert "vendor_daemon" not in _by_key(evaluate(_healthy()))


# ---- ladder ordering ----------------------------------------------------


def test_ladder_runs_hardware_outward():
    """Root cause should appear above symptom when read top-to-bottom."""
    probe = _healthy(
        runtime_status="suspended",
        modules_loaded=ALL_MODULES - {"nvidia_uvm"},
        container_smi_ok=False,
    )
    keys = [c.key for c in evaluate(probe)]
    assert keys.index("power_state") < keys.index("modules")
    assert keys.index("modules") < keys.index("device_nodes")
    assert keys.index("device_nodes") < keys.index("container_gpu")


# ---- fix planning -------------------------------------------------------


def test_fix_argv_prefixes_sudo_only_when_needed():
    root_fix = Fix(description="x", argv=[["nvidia-ctk", "cdi", "generate"]], needs_root=True)
    user_fix = Fix(description="y", argv=[["nvidia-smi", "-L"]], needs_root=False)
    assert fix_argv(root_fix) == [["sudo", "nvidia-ctk", "cdi", "generate"]]
    assert fix_argv(user_fix) == [["nvidia-smi", "-L"]]
    assert fix_argv(root_fix, use_sudo=False) == [["nvidia-ctk", "cdi", "generate"]]


def test_fix_argv_does_not_mutate_the_fix():
    f = Fix(description="x", argv=[["modprobe", "nvidia_uvm"]], needs_root=True)
    fix_argv(f)
    fix_argv(f)
    assert f.argv == [["modprobe", "nvidia_uvm"]]


def test_autofixable_excludes_manual_and_passing_checks():
    probe = _healthy(
        runtime_status="suspended",  # auto
        drm_modeset=False,  # manual
        cdi_spec_present=False,  # auto
    )
    checks = evaluate(probe)
    keys = {c.key for c in autofixable(checks)}
    assert keys == {"power_state", "cdi_spec"}
    assert {c.key for c in manual_actions(checks)} == {"drm_modeset"}


def test_apply_fix_dry_run_runs_nothing():
    f = Fix(description="x", argv=[["false"]], needs_root=False)
    ok, lines = apply_fix(f, dry_run=True)
    assert ok
    assert all(line.startswith("[dry-run]") for line in lines)


def test_apply_fix_stops_at_first_failure():
    f = Fix(description="x", argv=[["false"], ["echo", "unreachable"]], needs_root=False)
    ok, lines = apply_fix(f, use_sudo=False)
    assert not ok
    assert not any("unreachable" in line for line in lines)


def test_apply_fix_refuses_manual_fixes():
    ok, lines = apply_fix(Fix(description="x", auto=False, manual_hint="do it yourself"))
    assert not ok
    assert lines == ["not auto-fixable"]


# ---- serialization ------------------------------------------------------


def test_checks_serialize_for_json_mode():
    for check in evaluate(_healthy(cdi_spec_present=False)):
        d = check.to_dict()
        assert set(d) == {"key", "title", "severity", "detail", "fix"}
        if d["fix"]:
            assert "commands" in d["fix"]
            assert all(isinstance(c, str) for c in d["fix"]["commands"])


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, "ok"),
        ({"runtime_status": "suspended"}, "warn"),
        ({"cdi_spec_present": False}, "fail"),
        ({"drm_modeset": False, "cdi_spec_present": False}, "fail"),
    ],
)
def test_overall_verdict(overrides, expected):
    assert summarize(evaluate(_healthy(**overrides)))["overall"] == expected


# ---- kernel-log diagnosis (the "active but nvidia-smi sees nothing" case) --


def test_power_state_not_ok_when_driver_cannot_open_the_gpu():
    """Reported from a real Pop!_OS box: OmniLab said the GPU was active
    while nvidia-smi said "No devices were found". Both were true — the
    kernel's PCI view and NVML's view are different layers — but a green
    tick above a failed driver check reads as a contradiction.
    """
    probe = _healthy(runtime_status="active", nvidia_smi_ok=False)
    checks = _by_key(evaluate(probe))
    assert checks["power_state"].severity == "warn"
    assert "driver cannot open it" in checks["power_state"].detail
    assert checks["driver"].severity == "fail"


def test_power_state_ok_only_when_driver_also_works():
    assert _by_key(evaluate(_healthy()))["power_state"].severity == "ok"


def test_rm_init_adapter_is_named_with_a_cold_boot_hint():
    probe = _healthy(
        nvidia_smi_ok=False,
        kernel_log_readable=True,
        nvrm_lines=("NVRM: GPU 0000:01:00.0: RmInitAdapter failed! (0x25:0x65:1245)",),
    )
    driver = _by_key(evaluate(probe))["driver"]
    assert "could not initialize" in driver.detail
    assert "RmInitAdapter failed" in driver.detail
    assert driver.fix is not None
    # A warm reboot does not reset the GPU's power state; say so explicitly.
    assert "Cold boot" in driver.fix.manual_hint


def test_api_mismatch_outranks_init_failure():
    """Signature order is specificity, not log order — a version mismatch
    explains an init failure, so it must win even when it appears later."""
    probe = _healthy(
        nvidia_smi_ok=False,
        kernel_log_readable=True,
        nvrm_lines=(
            "NVRM: GPU 0000:01:00.0: RmInitAdapter failed!",
            "NVRM: API mismatch: the client has the version 550.120, but this kernel "
            "module has the version 580.65.06.",
        ),
    )
    driver = _by_key(evaluate(probe))["driver"]
    assert "different driver versions" in driver.detail


def test_nouveau_conflict_is_named():
    probe = _healthy(
        nvidia_smi_ok=False,
        kernel_log_readable=True,
        nvrm_lines=("NVRM: The NVIDIA probe routine was not called for 1 device(s).",),
    )
    driver = _by_key(evaluate(probe))["driver"]
    assert "another driver claimed the device" in driver.detail
    assert "nouveau" in driver.fix.manual_hint


def test_fallen_off_the_bus_is_named():
    probe = _healthy(
        nvidia_smi_ok=False,
        kernel_log_readable=True,
        nvrm_lines=("NVRM: GPU 0000:01:00.0: GPU has fallen off the bus.",),
    )
    assert "stopped responding on the PCI bus" in _by_key(evaluate(probe))["driver"].detail


def test_unreadable_kernel_log_is_not_reported_as_no_errors():
    """dmesg is restricted on Ubuntu/Pop!_OS. Not being able to look must
    never be presented as having looked and found nothing."""
    probe = _healthy(nvidia_smi_ok=False, kernel_log_readable=False, nvrm_lines=())
    hint = _by_key(evaluate(probe))["driver"].fix.manual_hint
    assert "could not be read" in hint
    assert "sudo dmesg" in hint


def test_readable_log_with_no_nvrm_lines_suggests_driver_absent():
    probe = _healthy(nvidia_smi_ok=False, kernel_log_readable=True, nvrm_lines=())
    hint = _by_key(evaluate(probe))["driver"].fix.manual_hint
    assert "isn't installed at all" in hint


def test_classify_nvrm_returns_none_for_unrelated_lines():
    assert classify_nvrm(("NVRM: loading NVIDIA UNIX x86_64 Kernel Module 580.65.06",)) is None
    assert classify_nvrm(()) is None


def test_classify_nvrm_is_case_insensitive():
    assert classify_nvrm(("nvrm: gpu 0000:01:00.0: rm_init_adapter failed!",)) is not None


# ---- CDI spec ↔ podman parser compatibility ------------------------------


def _compat_probe(**overrides):
    base = {"cdi_has_additional_gids": True, "podman_version": (4, 9)}
    base.update(overrides)
    return _healthy(**base)


def test_cdi_gids_with_old_podman_warns_with_autofix():
    """Proven on Pop!_OS: podman 4.9 strict-rejects additionalGids, discards
    the whole spec, and containers die with 'unresolvable CDI devices' while
    nvidia-ctk cdi list looks perfectly healthy."""
    checks = _by_key(evaluate(_compat_probe()))
    c = checks["cdi_compat"]
    assert c.severity == "warn"
    assert "unresolvable CDI devices" in c.detail
    assert c.fix is not None and c.fix.auto and c.fix.needs_root
    # Strip script runs under the CLI's own interpreter (guaranteed PyYAML),
    # not the bare system python3.
    import sys

    assert c.fix.argv[0][0] == sys.executable
    assert any("nvidia-cdi-refresh" in " ".join(a) for a in c.fix.argv)


def test_cdi_compat_silent_on_new_podman():
    assert "cdi_compat" not in _by_key(evaluate(_compat_probe(podman_version=(5, 2))))


def test_cdi_compat_silent_without_gids():
    assert "cdi_compat" not in _by_key(evaluate(_compat_probe(cdi_has_additional_gids=False)))


def test_cdi_compat_silent_when_unprobed():
    """None means we couldn't read the spec or podman — don't invent a verdict."""
    assert "cdi_compat" not in _by_key(evaluate(_compat_probe(podman_version=None)))
    assert "cdi_compat" not in _by_key(evaluate(_compat_probe(cdi_has_additional_gids=None)))


def test_cdi_compat_autofixable():
    checks = evaluate(_compat_probe())
    assert "cdi_compat" in {c.key for c in autofixable(checks)}
