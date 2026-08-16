"""GPU diagnosis + repair for `omnilab gpu`.

Per project-spec-v1.md (rev 5) § "Install behavior": OmniLab no longer ships
the NVIDIA driver, so the seam we own is *diagnosis and repair* on whatever
host the user already has.

The failure this module exists for is almost never "the driver isn't
installed". It's that the driver is installed and **nothing is using it**:

- the dGPU is runtime-suspended (D3cold) and never woken,
- `nvidia_uvm` loads lazily and isn't loaded, so CUDA sees nothing,
- `/dev/nvidia*` nodes were never created,
- the CDI spec at /etc/cdi/nvidia.yaml is stale after a driver update,
- or everything above is fine and the app still renders on llvmpipe
  because no PRIME offload variables were set.

Structure mirrors `pair.py`: `evaluate()` is pure over a `GpuProbe` and is
fully testable; the live probing and fix-application paths are split out and
easy to mock.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["ok", "warn", "fail"]

# Kernel modules the CUDA + container path needs. `nvidia_uvm` is loaded
# lazily by the driver on first CUDA use — a machine can look perfectly
# healthy in nvidia-smi and still fail inside a container without it.
REQUIRED_MODULES: tuple[str, ...] = ("nvidia", "nvidia_modeset", "nvidia_drm", "nvidia_uvm")

# Device nodes created by nvidia-modprobe. /dev/nvidia0 is per-GPU and is
# checked separately via a glob so multi-GPU hosts don't false-fail.
REQUIRED_NODES: tuple[str, ...] = ("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-modeset")

# Software rasterisers. If the container reports one of these as its GL
# renderer, Gazebo is running on the CPU no matter what nvidia-smi says.
SOFTWARE_RENDERERS: tuple[str, ...] = ("llvmpipe", "softpipe", "swrast", "zink")

# Vendor daemons that gate the dGPU behind their own power/mux policy. We
# never fight these — we name them and hand the user the switch command.
VENDOR_DAEMONS: dict[str, str] = {
    "system76-power": "sudo system76-power graphics nvidia && reboot",
    "supergfxd": "supergfxctl -m Hybrid   # then log out",
    "optimus-manager": "optimus-manager --switch nvidia",
    "bumblebeed": "Bumblebee is legacy and conflicts with PRIME offload; consider removing it.",
}

# PRIME render offload. Without these the GL/Vulkan stack on a hybrid
# laptop silently picks the iGPU (or llvmpipe) even when the dGPU is awake
# and correctly passed through. This is *the* "driver works, GPU unused"
# bug — see podman.build_run_args, which applies these for gpu="nvidia".
PRIME_ENV: dict[str, str] = {
    "__NV_PRIME_RENDER_OFFLOAD": "1",
    "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
    "__VK_LAYER_NV_optimus": "NVIDIA_only",
}


# ---- fix + check records -------------------------------------------------


@dataclass
class Fix:
    """A remedy for a failed check.

    `auto=True` means we can run `argv` ourselves. `auto=False` means the
    fix needs something we can't do from a CLI — a reboot, a BIOS change,
    MOK enrollment at the boot prompt — and `manual_hint` carries the
    instructions instead.
    """

    description: str
    argv: list[list[str]] = field(default_factory=list)
    needs_root: bool = False
    auto: bool = True
    manual_hint: str = ""

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "commands": [" ".join(a) for a in self.argv],
            "needs_root": self.needs_root,
            "auto": self.auto,
            "manual_hint": self.manual_hint,
        }


@dataclass
class Check:
    key: str
    title: str
    severity: Severity
    detail: str = ""
    fix: Fix | None = None

    @property
    def ok(self) -> bool:
        return self.severity == "ok"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "severity": self.severity,
            "detail": self.detail,
            "fix": self.fix.to_dict() if self.fix else None,
        }


# ---- probe ---------------------------------------------------------------


@dataclass
class GpuProbe:
    """Raw host readings.

    Every field defaults safe-pessimistic so a test overrides only what it
    cares about, same convention as `pair.NetworkProbe`.

    `container_smi_ok` / `container_renderer` are None when the in-container
    stage was skipped (no image, podman missing, --no-container).
    """

    pci_present: bool = False
    pci_address: str = ""
    pci_name: str = ""
    runtime_status: str = ""  # "active" | "suspended" | "" (unknown)
    modules_loaded: frozenset[str] = frozenset()
    drm_modeset: bool | None = None
    device_nodes: frozenset[str] = frozenset()
    nvidia_smi_ok: bool = False
    driver_version: str = ""
    toolkit_present: bool = False
    cdi_spec_present: bool = False
    cdi_driver_version: str = ""
    secure_boot: bool | None = None  # None = couldn't tell
    vendor_daemons: frozenset[str] = frozenset()
    container_smi_ok: bool | None = None
    container_renderer: str | None = None


# ---- pure evaluation -----------------------------------------------------


def evaluate(probe: GpuProbe) -> list[Check]:  # noqa: PLR0912, PLR0915
    """Pure: turn a probe into an ordered check ladder.

    Order matters — it runs outward from the hardware to the application:
    bus → power → modules → nodes → driver → container plumbing → render.
    A user reading top-to-bottom hits the root cause before the symptom.
    """
    checks: list[Check] = []

    # 1. Is there a dGPU at all? Distinguishes "no GPU" from "asleep",
    #    which otherwise look identical from userspace.
    if not probe.pci_present:
        checks.append(
            Check(
                key="pci_present",
                title="NVIDIA GPU on PCI bus",
                severity="fail",
                detail="no NVIDIA device found in the PCI enumeration",
                fix=Fix(
                    description="Enable the discrete GPU in firmware",
                    auto=False,
                    manual_hint=(
                        "No NVIDIA GPU is visible on the PCI bus. If this machine has one, it is "
                        "likely disabled in BIOS/UEFI, or held by a MUX switch in 'integrated only' "
                        "mode. Check firmware settings for 'Discrete Graphics', 'Hybrid Graphics', "
                        "or 'MUX'. Nothing below can pass until this does."
                    ),
                ),
            )
        )
        return checks  # everything downstream is meaningless without a GPU

    checks.append(
        Check(
            key="pci_present",
            title="NVIDIA GPU on PCI bus",
            severity="ok",
            detail=probe.pci_name or probe.pci_address,
        )
    )

    # 2. Runtime power state. A suspended dGPU is *normal* on a laptop —
    #    it's the power-saving default, not a fault. It only becomes a
    #    problem when nothing wakes it. Querying nvidia-smi does.
    if probe.runtime_status == "suspended":
        checks.append(
            Check(
                key="power_state",
                title="GPU power state",
                severity="warn",
                detail="runtime-suspended (D3cold) — normal when idle, but nothing has woken it",
                fix=Fix(
                    description="Wake the GPU by querying it",
                    argv=[["nvidia-smi", "-L"]],
                ),
            )
        )
    elif probe.runtime_status == "active":
        checks.append(Check(key="power_state", title="GPU power state", severity="ok", detail="active"))
    else:
        checks.append(
            Check(
                key="power_state",
                title="GPU power state",
                severity="warn",
                detail="could not read runtime_status (kernel may not expose runtime PM for this device)",
            )
        )

    # 3. Kernel modules.
    missing_modules = [m for m in REQUIRED_MODULES if m not in probe.modules_loaded]
    if missing_modules:
        # Secure Boot rejecting unsigned modules is the one cause that
        # modprobe cannot fix — call it out rather than looping the user.
        if probe.secure_boot:
            checks.append(
                Check(
                    key="modules",
                    title="NVIDIA kernel modules",
                    severity="fail",
                    detail=f"missing: {', '.join(missing_modules)} (Secure Boot is enabled)",
                    fix=Fix(
                        description="Enroll the NVIDIA module signing key",
                        auto=False,
                        manual_hint=(
                            "Secure Boot is on and NVIDIA modules are not loaded — the kernel is "
                            "almost certainly refusing unsigned modules. Either enroll the module "
                            "signing key with `sudo mokutil --import /var/lib/dkms/mok.pub` (then "
                            "reboot and confirm at the blue MOK prompt), or disable Secure Boot in "
                            "firmware. This cannot be fixed from userspace."
                        ),
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    key="modules",
                    title="NVIDIA kernel modules",
                    severity="fail",
                    detail=f"missing: {', '.join(missing_modules)}",
                    fix=Fix(
                        description=f"Load {len(missing_modules)} missing module(s)",
                        argv=[["modprobe", m] for m in missing_modules],
                        needs_root=True,
                    ),
                )
            )
    else:
        checks.append(
            Check(
                key="modules",
                title="NVIDIA kernel modules",
                severity="ok",
                detail=f"all loaded ({', '.join(REQUIRED_MODULES)})",
            )
        )

    # 4. DRM modesetting — required for Wayland, and for Gazebo's GL
    #    context on a hybrid laptop. Needs a kernel cmdline change.
    if probe.drm_modeset is False:
        checks.append(
            Check(
                key="drm_modeset",
                title="nvidia-drm modesetting",
                severity="warn",
                detail="nvidia_drm.modeset=0 — Wayland sessions and GL offload may fall back to the iGPU",
                fix=Fix(
                    description="Enable DRM modesetting on the kernel cmdline",
                    auto=False,
                    manual_hint=(
                        "Add `nvidia-drm.modeset=1` to your kernel command line and reboot.\n"
                        "  GRUB:      add to GRUB_CMDLINE_LINUX in /etc/default/grub, then "
                        "`sudo grub2-mkconfig -o /boot/grub2/grub.cfg`\n"
                        "  systemd-boot: add to the options line in /boot/loader/entries/*.conf\n"
                        "  Fedora:    `sudo grubby --update-kernel=ALL --args=nvidia-drm.modeset=1`"
                    ),
                ),
            )
        )
    elif probe.drm_modeset:
        checks.append(Check(key="drm_modeset", title="nvidia-drm modesetting", severity="ok", detail="enabled"))

    # 5. Device nodes. nvidia-modprobe is setuid root by design, so this
    #    one auto-fixes without asking for sudo.
    has_gpu_node = any(re.fullmatch(r"/dev/nvidia\d+", n) for n in probe.device_nodes)
    missing_nodes = [n for n in REQUIRED_NODES if n not in probe.device_nodes]
    if missing_nodes or not has_gpu_node:
        detail = f"missing: {', '.join(missing_nodes)}" if missing_nodes else "no /dev/nvidiaN device node"
        checks.append(
            Check(
                key="device_nodes",
                title="/dev/nvidia* device nodes",
                severity="fail",
                detail=detail,
                fix=Fix(
                    description="Create the device nodes",
                    argv=[["nvidia-modprobe", "-c", "0", "-u"]],
                ),
            )
        )
    else:
        checks.append(
            Check(
                key="device_nodes",
                title="/dev/nvidia* device nodes",
                severity="ok",
                detail=f"{len(probe.device_nodes)} nodes present",
            )
        )

    # 6. Does the driver actually answer?
    if probe.nvidia_smi_ok:
        checks.append(
            Check(
                key="driver",
                title="NVIDIA driver responds",
                severity="ok",
                detail=f"driver {probe.driver_version}" if probe.driver_version else "nvidia-smi ok",
            )
        )
    else:
        checks.append(
            Check(
                key="driver",
                title="NVIDIA driver responds",
                severity="fail",
                detail="nvidia-smi failed or is not installed",
                fix=Fix(
                    description="Install or repair the NVIDIA driver",
                    auto=False,
                    manual_hint=(
                        "nvidia-smi did not return. If the modules above loaded, this is usually a "
                        "driver/userspace-library version mismatch after a partial upgrade — "
                        "reinstall the driver package for your distro and reboot."
                    ),
                ),
            )
        )

    # 7. Vendor power daemons. Detect and name; never fight them.
    if probe.vendor_daemons:
        names = sorted(probe.vendor_daemons)
        hints = "\n".join(f"  {n}: {VENDOR_DAEMONS.get(n, 'check its docs')}" for n in names)
        checks.append(
            Check(
                key="vendor_daemon",
                title="Vendor GPU power daemon",
                severity="warn",
                detail=f"active: {', '.join(names)}",
                fix=Fix(
                    description="Switch the vendor daemon to a GPU-enabled profile",
                    auto=False,
                    manual_hint=(
                        "A vendor daemon is managing GPU power and may be holding the dGPU off "
                        f"regardless of anything OmniLab does:\n{hints}"
                    ),
                ),
            )
        )

    # 8. Container toolkit.
    if not probe.toolkit_present:
        checks.append(
            Check(
                key="toolkit",
                title="nvidia-container-toolkit",
                severity="fail",
                detail="nvidia-ctk not found on PATH",
                fix=Fix(
                    description="Install nvidia-container-toolkit",
                    auto=False,
                    manual_hint=(
                        "Install the NVIDIA container toolkit for your distro:\n"
                        "  Fedora/RHEL: sudo dnf install -y nvidia-container-toolkit\n"
                        "  Ubuntu/Debian: sudo apt-get install -y nvidia-container-toolkit\n"
                        "  Arch: sudo pacman -S nvidia-container-toolkit\n"
                        "See https://docs.nvidia.com/datacenter/cloud-native/ for repo setup."
                    ),
                ),
            )
        )
    else:
        checks.append(Check(key="toolkit", title="nvidia-container-toolkit", severity="ok", detail="nvidia-ctk found"))

        # 9. CDI spec — present and matching the running driver. A stale
        #    spec after a driver update is the most common way a working
        #    GPU disappears from containers.
        if not probe.cdi_spec_present:
            checks.append(
                Check(
                    key="cdi_spec",
                    title="CDI spec (/etc/cdi/nvidia.yaml)",
                    severity="fail",
                    detail="no CDI spec — podman cannot resolve nvidia.com/gpu=all",
                    fix=Fix(
                        description="Generate the CDI spec",
                        argv=[["nvidia-ctk", "cdi", "generate", "--output=/etc/cdi/nvidia.yaml"]],
                        needs_root=True,
                    ),
                )
            )
        elif (
            probe.cdi_driver_version
            and probe.driver_version
            and probe.cdi_driver_version != probe.driver_version
        ):
            checks.append(
                Check(
                    key="cdi_spec",
                    title="CDI spec (/etc/cdi/nvidia.yaml)",
                    severity="fail",
                    detail=(
                        f"stale — spec was generated for driver {probe.cdi_driver_version}, "
                        f"running driver is {probe.driver_version}"
                    ),
                    fix=Fix(
                        description="Regenerate the CDI spec for the current driver",
                        argv=[["nvidia-ctk", "cdi", "generate", "--output=/etc/cdi/nvidia.yaml"]],
                        needs_root=True,
                    ),
                )
            )
        else:
            checks.append(
                Check(
                    key="cdi_spec",
                    title="CDI spec (/etc/cdi/nvidia.yaml)",
                    severity="ok",
                    detail=f"present, driver {probe.cdi_driver_version}" if probe.cdi_driver_version else "present",
                )
            )

    # 10. End-to-end: does the container see the GPU?
    if probe.container_smi_ok is not None:
        if probe.container_smi_ok:
            checks.append(
                Check(
                    key="container_gpu",
                    title="GPU visible inside container",
                    severity="ok",
                    detail="nvidia-smi succeeded inside the project image",
                )
            )
        else:
            checks.append(
                Check(
                    key="container_gpu",
                    title="GPU visible inside container",
                    severity="fail",
                    detail="nvidia-smi failed inside the project image",
                    fix=Fix(
                        description="Regenerate the CDI spec, then retry",
                        argv=[["nvidia-ctk", "cdi", "generate", "--output=/etc/cdi/nvidia.yaml"]],
                        needs_root=True,
                    ),
                )
            )

    # 11. The check that catches the bug this module was written for:
    #     everything above green, and the app still renders on the CPU.
    if probe.container_renderer is not None:
        renderer = probe.container_renderer
        low = renderer.lower()
        if any(sw in low for sw in SOFTWARE_RENDERERS):
            checks.append(
                Check(
                    key="render_offload",
                    title="PRIME render offload",
                    severity="fail",
                    detail=f"container GL renderer is '{renderer}' — rendering on the CPU",
                    fix=Fix(
                        description="Launch with PRIME offload variables set",
                        auto=False,
                        manual_hint=(
                            "The GPU is passed through but the GL stack isn't using it. `omnilab up` "
                            "sets these automatically for gpu=nvidia — if you see this, the container "
                            "was started another way or predates that fix. Recreate it with "
                            "`omnilab down && omnilab up`. The variables are:\n"
                            + "\n".join(f"  {k}={v}" for k, v in PRIME_ENV.items())
                        ),
                    ),
                )
            )
        elif "nvidia" in low:
            checks.append(
                Check(
                    key="render_offload",
                    title="PRIME render offload",
                    severity="ok",
                    detail=f"container GL renderer is '{renderer}'",
                )
            )
        else:
            checks.append(
                Check(
                    key="render_offload",
                    title="PRIME render offload",
                    severity="warn",
                    detail=f"container GL renderer is '{renderer}' — not NVIDIA, but not a known software rasteriser",
                )
            )

    return checks


def summarize(checks: list[Check]) -> dict:
    """Pure: counts + a single overall verdict for `--json` consumers."""
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for c in checks:
        counts[c.severity] += 1
    if counts["fail"]:
        overall = "fail"
    elif counts["warn"]:
        overall = "warn"
    else:
        overall = "ok"
    return {"overall": overall, "counts": counts, "total": len(checks)}


def autofixable(checks: list[Check]) -> list[Check]:
    """Pure: the subset we can repair ourselves, in ladder order."""
    return [c for c in checks if not c.ok and c.fix is not None and c.fix.auto and c.fix.argv]


def manual_actions(checks: list[Check]) -> list[Check]:
    """Pure: failures that need a human — reboot, firmware, MOK enrollment."""
    return [c for c in checks if not c.ok and c.fix is not None and not c.fix.auto]


def fix_argv(fix: Fix, *, use_sudo: bool = True) -> list[list[str]]:
    """Pure: prefix root-requiring commands with sudo when we aren't root."""
    if not fix.needs_root or not use_sudo:
        return [list(a) for a in fix.argv]
    return [["sudo", *a] for a in fix.argv]


# ---- live probing (best-effort; mocked in tests) ------------------------


def _run(argv: list[str], *, timeout: float = 10.0) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
        return 127, ""
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _probe_pci() -> tuple[bool, str, str]:
    rc, out = _run(["lspci", "-D", "-nn"])
    if rc != 0:
        return False, "", ""
    for line in out.splitlines():
        # Vendor 10de is NVIDIA; match VGA/3D controller classes only so we
        # don't trip on an NVIDIA audio function.
        if "10de:" in line.lower() and ("vga" in line.lower() or "3d controller" in line.lower()):
            addr = line.split()[0]
            return True, addr, line.strip()
    return False, "", ""


def _probe_runtime_status(pci_address: str) -> str:
    if not pci_address:
        return ""
    p = Path(f"/sys/bus/pci/devices/{pci_address}/power/runtime_status")
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def _probe_modules() -> frozenset[str]:
    try:
        text = Path("/proc/modules").read_text()
    except OSError:
        return frozenset()
    return frozenset(line.split()[0] for line in text.splitlines() if line.strip())


def _probe_drm_modeset() -> bool | None:
    p = Path("/sys/module/nvidia_drm/parameters/modeset")
    try:
        return p.read_text().strip().upper().startswith("Y")
    except OSError:
        return None


def _probe_device_nodes() -> frozenset[str]:
    try:
        return frozenset(str(p) for p in Path("/dev").glob("nvidia*"))
    except OSError:
        return frozenset()


def _probe_driver_version() -> tuple[bool, str]:
    rc, out = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"], timeout=30.0)
    if rc != 0:
        return False, ""
    return True, out.strip().splitlines()[0].strip() if out.strip() else ""


_CDI_VERSION_RE = re.compile(r"(?:driver[._-]?version|nvidia\.driver\.version)\D{0,4}([\d.]+)", re.IGNORECASE)


def _probe_cdi() -> tuple[bool, str]:
    p = Path("/etc/cdi/nvidia.yaml")
    if not p.exists():
        p = Path("/var/run/cdi/nvidia.yaml")
    if not p.exists():
        return False, ""
    try:
        text = p.read_text()
    except OSError:
        return True, ""
    m = _CDI_VERSION_RE.search(text)
    return True, m.group(1) if m else ""


def _probe_secure_boot() -> bool | None:
    rc, out = _run(["mokutil", "--sb-state"])
    if rc != 0:
        return None
    low = out.lower()
    if "enabled" in low:
        return True
    if "disabled" in low:
        return False
    return None


def _probe_vendor_daemons() -> frozenset[str]:
    active = set()
    for name in VENDOR_DAEMONS:
        rc, _ = _run(["systemctl", "is-active", "--quiet", name], timeout=5.0)
        if rc == 0:
            active.add(name)
    return frozenset(active)


def probe_host(*, wake: bool = True) -> GpuProbe:
    """Read every host-side signal.

    `wake=True` queries nvidia-smi first, which forces a runtime resume out
    of D3cold. We read the power state *before* that so the report shows
    the state the user actually found the machine in, not the one we
    created by looking.
    """
    pci_present, pci_address, pci_name = _probe_pci()
    runtime_status = _probe_runtime_status(pci_address)

    if wake and pci_present:
        _run(["nvidia-smi", "-L"], timeout=30.0)

    smi_ok, driver_version = _probe_driver_version()
    cdi_present, cdi_version = _probe_cdi()

    return GpuProbe(
        pci_present=pci_present,
        pci_address=pci_address,
        pci_name=pci_name,
        runtime_status=runtime_status,
        modules_loaded=_probe_modules(),
        drm_modeset=_probe_drm_modeset(),
        device_nodes=_probe_device_nodes(),
        nvidia_smi_ok=smi_ok,
        driver_version=driver_version,
        toolkit_present=shutil.which("nvidia-ctk") is not None,
        cdi_spec_present=cdi_present,
        cdi_driver_version=cdi_version,
        secure_boot=_probe_secure_boot(),
        vendor_daemons=_probe_vendor_daemons(),
    )


def probe_container(image: str, *, timeout: float = 120.0) -> tuple[bool | None, str | None]:
    """Run nvidia-smi and glxinfo inside `image` — the only checks that
    prove the whole chain works. Returns (smi_ok, renderer); either is
    None when the stage couldn't run at all.
    """
    if shutil.which("podman") is None:
        return None, None

    rc, _ = _run(
        ["podman", "run", "--rm", "--device", "nvidia.com/gpu=all", image, "nvidia-smi", "-L"],
        timeout=timeout,
    )
    if rc == 127:
        return None, None
    smi_ok = rc == 0

    env_args: list[str] = []
    for k, v in PRIME_ENV.items():
        env_args += ["-e", f"{k}={v}"]
    rc2, out2 = _run(
        [
            "podman", "run", "--rm", "--device", "nvidia.com/gpu=all",
            *env_args,
            image, "bash", "-lc", "glxinfo -B 2>/dev/null | grep -i 'OpenGL renderer'",
        ],
        timeout=timeout,
    )
    renderer = None
    if rc2 == 0 and out2.strip():
        _, _, tail = out2.partition(":")
        renderer = tail.strip() or out2.strip()

    return smi_ok, renderer


def apply_fix(fix: Fix, *, dry_run: bool = False, use_sudo: bool = True) -> tuple[bool, list[str]]:
    """Run a fix's commands in order, stopping at the first failure.

    Returns (all_succeeded, transcript_lines). Never raises — the caller
    decides how to surface a partial repair.
    """
    lines: list[str] = []
    if not fix.auto or not fix.argv:
        return False, ["not auto-fixable"]

    for argv in fix_argv(fix, use_sudo=use_sudo):
        printed = " ".join(argv)
        if dry_run:
            lines.append(f"[dry-run] {printed}")
            continue
        rc, out = _run(argv, timeout=120.0)
        if rc == 0:
            lines.append(f"[ok] {printed}")
        else:
            lines.append(f"[failed rc={rc}] {printed}")
            if out.strip():
                lines.append(out.strip().splitlines()[-1])
            return False, lines
    return True, lines
