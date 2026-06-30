"""The client-facing HTTP service (ADR-0018).

One versioned ``/v1`` HTTP contract carrying the Canonical IR, bound to a Unix
socket locally (default, no auth — the OS enforces it via filesystem perms) or a
TCP port remotely. Work is modeled as server-owned async jobs that survive client
disconnect. Both this service and the CLI drive ``transcribbler.pipeline``.

Out of scope here (each its own task): the VRAM-budget scheduler + live mode
(ADR-0019 — jobs run FIFO single-flight), and TCP bearer-token/TLS auth
(ADR-0020 — only the UDS transport is wired).
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
