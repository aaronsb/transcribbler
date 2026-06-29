"""Capability probe (ADR-0015).

Detect which GPU backends are present so a host can self-select / recommend a
profile. Pure detection — no model loading.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    cuda: str | None
    rocm: str | None
    vulkan: str | None
    cpu: bool = True

    def recommend(self) -> str:
        """Vulkan-first portable default (ADR-0015); native paths are opt-in."""
        if self.vulkan:
            return "vulkan"
        if self.cuda:
            return "cuda"
        if self.rocm:
            return "rocm"
        return "cpu"


def _first_line(cmd: list[str]) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


def detect() -> Capabilities:
    cuda = (
        _first_line(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        if shutil.which("nvidia-smi")
        else None
    )
    rocm = None
    if shutil.which("rocminfo"):
        # Pick the GPU agent's marketing name, not the CPU agent's (listed first).
        out = _first_line(
            [
                "bash",
                "-c",
                "rocminfo | grep 'Marketing Name' | grep -iE 'radeon|instinct|gpu' | head -1 | cut -d: -f2",
            ]
        )
        rocm = out or "detected"
    vulkan = None
    if shutil.which("vulkaninfo"):
        out = _first_line(
            ["bash", "-c", "vulkaninfo --summary 2>/dev/null | grep -m1 deviceName | cut -d= -f2"]
        )
        vulkan = (out or "detected").strip()
    return Capabilities(cuda=cuda, rocm=rocm, vulkan=vulkan)
