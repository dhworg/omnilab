"""GPU detection — pick NVIDIA passthrough vs iGPU rendering for `omnilab up`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal

GpuKind = Literal["nvidia", "igpu", "none"]


# A dGPU resuming from runtime suspend (D3cold) routinely takes longer than
# a few seconds — on some laptops 10-20s. The old 5s timeout meant a healthy
# NVIDIA machine that happened to be idle got misreported as iGPU-only, and
# `omnilab up` then launched without GPU passthrough. Querying nvidia-smi is
# also what *wakes* the GPU, so this call has to be allowed to finish.
_SMI_WAKE_TIMEOUT_SECONDS = 30


def detect_gpu() -> GpuKind:
    """Best-effort detection. Returns 'nvidia' if an NVIDIA GPU is reachable
    via nvidia-smi, 'igpu' if /dev/dri exists (Intel/AMD integrated or any
    KMS-managed GPU), else 'none'.

    Note this is deliberately a *coarse* check: it answers "should we pass a
    GPU through", not "will rendering actually land on it". `omnilab gpu`
    (gpu_doctor.py) answers the second question.
    """
    if shutil.which("nvidia-smi"):
        try:
            subprocess.run(
                ["nvidia-smi", "-L"],
                check=True,
                capture_output=True,
                timeout=_SMI_WAKE_TIMEOUT_SECONDS,
            )
            return "nvidia"
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if Path("/dev/dri").exists() and any(Path("/dev/dri").iterdir()):
        return "igpu"

    return "none"


def resolve_gpu_mode(manifest_mode: str) -> GpuKind:
    """Map manifest `gpu:` setting + host detection to a concrete kind.

    - 'auto' uses detect_gpu()
    - 'igpu' or 'nvidia' force that mode (even if absent — caller can
      still fail later, but the user explicitly asked for it).
    """
    if manifest_mode == "auto":
        return detect_gpu()
    if manifest_mode == "nvidia":
        return "nvidia"
    if manifest_mode == "igpu":
        return "igpu"
    msg = f"unknown gpu mode: {manifest_mode!r}"
    raise ValueError(msg)
