# ADR-0030: Installer & service lifecycle — editable uv-tool install + a systemd --user unit

- **Status**: Accepted
- **Date**: 2026-07-08
- **Decided by**: extends [ADR-0007](0007-supervisor-agnostic-packaging.md) (the reserved
  `packaging/` `.service`) and operationalizes it; the CLI-install half is new here.

## Context

[ADR-0007](0007-supervisor-agnostic-packaging.md) decided a **supervisor-agnostic service
process** plus thin per-host wrappers, and said `packaging/` would ship a `systemd --user`
`.service` file. That wrapper was never built. Meanwhile `make install` put a **hand-written
shim** on `$PATH`:

```sh
#!/usr/bin/env bash
exec uv run --project "/abs/path/to/repo/backend" transcribbler "$@"
```

Two problems compounded:

- The shim **hard-codes the repo path** and re-resolves the environment through `uv run` on
  every invocation. It is not an *installed* tool — it is a pointer at a working tree.
- It installed **only `transcribbler`**, never the `transcribbler-serve` daemon entrypoint —
  so the service ADR-0007 describes had no launcher at all, and no lifecycle
  (enable/start/stop/status).

A real constraint shapes the fix. The CLI resolves two things **relative to its own package
location**: the diarizer sidecar (`diarizer_daemon._SIDECAR_DIR = __file__/../../diarizer`,
run via `uv run --project backend/diarizer`) and the secrets file
(`env.DEFAULT_ENV_FILE = __file__/../../../.env.hf`). Any install that **relocates** the
package into an isolated `site-packages` (a normal, non-editable install) moves `__file__`
and **breaks both**.

## Decision

**Install the backend as an *editable* uv tool, and ship the `systemd --user` unit ADR-0007
reserved. The Makefile is the lifecycle surface.**

### 1 — Editable uv-tool install (both entrypoints)

`make install` runs `uv tool install --editable ./backend --force`, which places **both**
`transcribbler` and `transcribbler-serve` in `~/.local/bin` as uv-managed executables, each
shebanged into the tool's isolated venv — so they run without `uv run` in the loop, survive
the repo being on a different `$PATH`, and uninstall cleanly with `uv tool uninstall`.

**Editable is load-bearing, not a convenience.** It is the *only* install mode under which
`transcribbler.__file__` still points into the working tree, keeping the diarizer-sidecar and
`.env.hf` path resolution intact. The consequence is deliberate: the installed tool **depends
on the working tree staying put** (move it → re-run `make install`). We accept that coupling
because the sidecar is a separate pinned venv (ADR-0002) the main tool shells into by path;
decoupling it fully is a later packaging problem, not this one.

`uv sync --project backend/diarizer` still runs at install time to provision that sidecar venv.

### 2 — The systemd --user unit (ADR-0007's `packaging/`)

`packaging/transcribbler.service` is a `systemd --user` unit that runs the installed
`transcribbler-serve` (Unix socket under `$XDG_RUNTIME_DIR`, ADR-0018). It is supervisor-
agnostic per ADR-0007: `Type=simple`, logs to stdout→journald (no `sd_notify`, no journald-
only assumptions), `Restart=on-failure`, and an **optional** `EnvironmentFile=-%h/.config/
transcribbler/env` for `HF_TOKEN` (the `-` makes it non-fatal when absent). The same binary
can still run as PID 1 in a container; only the wrapper differs.

### 3 — The Makefile is the lifecycle

One verb surface, symmetric install/uninstall:

- **CLI**: `install`, `uninstall` (also removes the unit), `reinstall`, `status`, `update`.
- **service**: `service-install` (copy unit + `daemon-reload`), `service-enable`
  (`enable --now`), `service-stop`, `service-disable`, `service-status`, `service-logs`
  (journald), `service-uninstall`.
- **build**: `dist` (`uv build` → a wheel/sdist for installing elsewhere).

### Scope boundary

Decides the **native (systemd --user) lifecycle and the CLI install mechanism**. The
**container** wrapper ADR-0007 also names (Dockerfile + compose) stays **deferred** — the
native path is the immediate need, and the same supervisor-agnostic process drops into a
container unchanged when cube wants it.

## Consequences

### Good

- One `make install` yields **both** managed entrypoints in `~/.local/bin`; `make uninstall`
  removes them and the unit with no residue — the symmetric lifecycle the shim lacked.
- The service ADR-0007 described is **real and operable** (`service-enable`/`status`/`logs`).
- Editable keeps the diarizer sidecar and secrets resolution working with zero code change.
- A CI job can install, assert both entrypoints resolve, and uninstall on a clean runner.

### Bad / costs

- The installed tool is **coupled to the working tree** (editable): move/delete the repo and
  the CLI breaks until `make install` re-runs. Documented; the trade for keeping sidecar/env
  path resolution. A fully-relocatable install needs the sidecar packaged as a dependency,
  not shelled-to by path — deferred.
- `--force` overwrites a pre-existing `~/.local/bin/transcribbler` (e.g. the old shim). That
  is intended (this replaces it), but it will clobber an unrelated same-named binary.

### Neutral

- Container packaging remains a reserved, unbuilt slot (ADR-0007), as before.

## Realized (as built)

- `packaging/transcribbler.service` — the systemd --user unit.
- `Makefile` — `install` (editable uv tool + diarizer sync), `uninstall`/`reinstall`/`status`,
  the `service-*` group, and `dist`.
- `.github/workflows/install.yml` — installer smoke: install → both entrypoints resolve →
  uninstall.
- **Verified end-to-end, isolated** (`UV_TOOL_DIR`/`UV_TOOL_BIN_DIR` in a scratch prefix, the
  live `~/.local/bin` untouched): both executables install and run (`transcribbler --version`,
  `transcribbler-serve --help`); at runtime `transcribbler.__file__` resolves into the repo and
  the diarizer sidecar dir + `.env.hf` both resolve and exist — confirming the editable
  constraint holds.

## Alternatives considered

- **Non-editable uv-tool / pipx install.** Rejected: relocates the package into an isolated
  `site-packages`, moving `__file__` and breaking the diarizer-sidecar and `.env.hf` path
  resolution. Would force a code change to locate those by env/XDG instead — a bigger change
  than this increment, deferred with the "fully-relocatable install" work.
- **Keep the repo-path shim, just add `-serve` + service targets.** Rejected: leaves the tool
  a pointer-at-a-working-tree with `uv run` on every call, and no clean `uv tool uninstall`.
- **A hand-rolled `install.sh`/`uninstall.sh`.** Rejected: reimplements what `uv tool` already
  does (isolated venv, PATH shim, clean removal, versioning) with more surface to maintain.

## Related

- [ADR-0007](0007-supervisor-agnostic-packaging.md) — supervisor-agnostic process; the
  reserved `packaging/` `.service` this builds.
- [ADR-0018](0018-client-facing-wire.md) — the UDS the service binds by default.
- [ADR-0002](0002-hardware-topology.md) — the pinned diarizer venv the sidecar path targets.
