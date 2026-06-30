"""Caller identity (ADR-0018 / ADR-0020).

Every ``/v1/jobs/{id}`` operation runs as a *principal*, and a job is visible
only to its owner. Locally (the UDS default) the OS already authenticated the
caller via filesystem permissions on the per-user runtime dir, so there is one
local principal — the user the service runs as.

Real multi-user identity (UDS ``SO_PEERCRED`` uid, or a TCP bearer-token subject)
is ADR-0020's job; this module is the seam where it will plug in. The ownership
check + unguessable ids already hold the boundary regardless of how rich the
principal becomes.
"""

from __future__ import annotations

import os

from fastapi import Request


def local_principal() -> str:
    return f"uid:{os.getuid()}"


def get_principal(request: Request) -> str:
    """FastAPI dependency: the principal for this request.

    For now the bind determines it (UDS → the local user). Set at startup on
    ``app.state.principal``.
    """
    return request.app.state.principal
