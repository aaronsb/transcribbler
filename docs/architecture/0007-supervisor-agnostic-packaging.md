# ADR-0007: Supervisor-agnostic single process; systemd + container wrappers

- **Status**: Accepted
- **Date**: 2026-06-28
- **Deciders**: Aaron

## Context

The original ask was a **systemd** approach instead of Docker for *"less overhead."* In
discussion the requirement evolved: it should run **containerized on cube** (which already
runs docker/traefik) *and* **natively on a laptop that isn't always at home**. The
operator floated a "systemd-in-docker" container so one artifact runs both ways.

Running systemd as PID 1 in a container is heavy and fiddly (cgroup mounts, privileged
access) — it fights the low-overhead goal. The portability requirement is really
*"the same service runs under whatever supervisor each host already has."*

## Decision

Build **one supervisor-agnostic service process** that doesn't care what supervises it,
plus thin per-host wrappers:

- **Native (laptop/desktop):** a `systemd --user` unit wraps the process — low overhead,
  the original ask.
- **Containerized (cube):** the **same** process is **PID 1** in the container (no inner
  systemd); cube's existing docker + traefik supervises and TLS-terminates it.

The binary/venv and config are identical across hosts; only the wrapper differs.
`packaging/` ships a `.service` file and a `Dockerfile` + compose snippet.

The **Python ML components run in a pinned 3.11/3.12 venv or container**, not the host's
3.14 ([ADR-0002](0002-hardware-topology.md)). whisper.cpp is a native binary and avoids
the Python-version treadmill on the hot path.

## Consequences

### Good
- Write the service once; run it under systemd or docker unchanged.
- No systemd-in-docker tax; keeps the low-overhead property.
- Fits cube's existing docker/traefik without special-casing.

### Bad / costs
- Must keep the service free of supervisor-specific assumptions (no journald-only logging,
  no hard dependency on systemd notify) — log to stdout, configure via env/flags/files.

## Alternatives considered

- **Literal systemd-in-docker** — rejected as the default (heavy, fiddly); available later
  if multiple co-located units in one image are ever wanted, chosen deliberately.
- **Docker-only** — rejected; the laptop/native low-overhead case matters.
- **Native-only** — rejected; cube is docker-first and always-on.

## Related

- ADR-0001 (composable processes), ADR-0002 (Python version), ADR-0004 (llama-swap also
  supervised this way).
