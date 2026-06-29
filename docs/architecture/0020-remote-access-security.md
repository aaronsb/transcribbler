# ADR-0020: Remote access & security — per-locality access tiers

- **Status**: Proposed
- **Date**: 2026-06-29
- **Deciders**: Aaron

## Context

[ADR-0018](0018-client-facing-wire.md) settled the *local* case (UDS, OS file
perms, no app auth) and named bearer-token-over-TLS as the *cross-host* baseline,
but deferred the real remote-access model — who may reach a backend over a network,
and how — to a security ADR. This is it.

The scoping facts that rule out whole categories of answer:

- This is **not a SaaS, not multi-tenant, no public signup** (project non-goals).
  The remote case is *"a handful of devices I control reach a backend."* That is
  **machine-to-machine**, not user-login — so OAuth/OIDC user flows and account
  systems are the wrong shape and out of scope.
- The data is private audio/transcripts; **Goal #1 is local-first & private**, with
  cloud as *"optional, never required."* Any path where data leaves the machines is
  therefore an opt-in with a consent obligation ([ADR-0013](0013-retention-and-consent.md)).

The unifying realization: every access path still terminates in the **same Canonical
IR over the same client-facing contract** (ADR-0018/[0006](0006-canonical-ir-contract.md)).
So a backend — *including a cloud provider* — is just an **endpoint the client points
at** ([ADR-0015](0015-pluggable-compute-backends.md)'s swap seam). Security becomes a
**per-endpoint property**, not a redesign, and each tier can **reuse trust that
already exists** instead of inventing an auth system.

Threat model (brief): defend against anyone who can reach a listening port — LAN
neighbors, and the whole internet if exposed. At stake: compute theft (job
submission), privacy breach (reading transcripts), and DoS. Legitimate callers are a
small, known set of operator-controlled devices.

## Decision

**Access is tiered by locality; each tier reuses an existing trust mechanism, and the
backend never binds a public port by default.**

| Tier | Path | Transport + identity | Principal (job owner) |
|---|---|---|---|
| **Local** (same host) | UDS | OS file perms (ADR-0018) | connecting OS user (`SO_PEERCRED`) |
| **LAN** (same room / trusted machine) | **SSH tunnel** (`ssh -L`) to the backend's loopback/UDS | **SSH keys** (already provisioned to cube) | the SSH user |
| **Direct** (advanced/opt-in) | TCP to the backend | **bearer token over TLS**, **mTLS opt-in** | the account the token/cert maps to |

The **internet / away** tier — an authenticated Cloudflare Worker calling cloud
providers — is split into its own proposed, deferred decision:
[ADR-0021](0021-internet-cloud-worker.md). All tiers below keep compute on the local
GPU; only that deferred tier sends data off-machine.

- **The backend binds loopback / UDS by default — never `0.0.0.0`.** Reaching it from
  another machine is done by *tunneling* (SSH), not by exposing it. This makes "no
  public attack surface" the default, not a configuration the user must remember.
- **LAN = SSH.** Transport and identity in one mechanism that already exists; the
  client connects to a locally-forwarded socket. No new auth system, no open port.
  (An overlay network — Tailscale/WireGuard — is the documented alternative when many
  devices need to roam without per-hop `ssh -L`.)
- **Internet = an authed edge, not an exposed origin** — see [ADR-0021](0021-internet-cloud-worker.md)
  (deferred). The home GPU is never reached on that tier; it is the one path where data
  leaves the machines, so it is opt-in with consent.
- **Direct public TCP is the discouraged advanced path**, for users who insist on
  exposing a backend: bearer token over TLS (traefik terminates, ADR-0007), with
  **mTLS client certs** as the hardening upgrade.

**Token hygiene** (the bearer baseline, wherever used — direct TCP, client→worker):
tokens are **per-client** (one device revocable without re-keying the rest),
long-lived, stored in client config with tight file perms, **never logged**, and
rotatable/revocable. Short-lived tokens would need an issuer we don't have; out of
scope.

**Client model:** the client holds a set of **named endpoints**, each with its
transport + credential (local UDS; an SSH-tunneled socket; a worker URL + token).
Switching tiers is selecting an endpoint — no code path differs beyond the connector.

### Principal & accounts (multi-user, not multi-tenant)

The identity each tier already establishes **resolves to a principal (an account)** —
the OS uid on UDS, the SSH user on a tunnel, the token/cert's account on direct TCP.
**Every job is owned by the principal that submitted it.** This makes a single backend
safely serve **several trusted accounts at once** — the scheduler tracks per-requester
ownership ([ADR-0019](0019-job-scheduling.md)), a principal sees/cancels **its own**
jobs, and queue-position is reported per requester.

This is **multi-user, not multi-tenant**: a known, trusted set of accounts (people /
devices the operator controls) sharing one backend — *not* the public-signup,
isolated-tenant SaaS the non-goals reject. There is no account *provisioning* system;
an account simply *is* whatever identity the transport authenticated. Strong tenant
isolation, per-account quotas, and billing are explicitly out of scope.

## Consequences

### Good

- The default posture is **zero public attack surface** — the backend isn't reachable
  off-box without an explicit tunnel.
- Each tier **reuses existing trust** (OS perms, SSH keys, Cloudflare Access) instead
  of a bespoke auth stack — less to build, less to get wrong.
- One backend safely serves **several trusted accounts** — identity comes from the
  transport, so multi-user falls out of the tier model with no separate auth system.
- A principal sees/cancels only **its own** jobs; the queue is per-requester (ADR-0019).

### Bad / costs

- SSH tunneling is slightly manual per device (mitigated by client endpoint config /
  `ssh -L` automation, or an overlay network).
- mTLS, if adopted, brings a private CA and per-device cert provisioning.
- "Multi-user, not multi-tenant" is a real but **soft** boundary — no strong
  isolation/quotas — so a backend should only be shared among genuinely trusted accounts.

## Alternatives considered

- **Expose the backend directly to the internet with a login / OAuth** — rejected:
  wrong shape (M2M, not user accounts), and a permanent public attack surface for a
  personal tool.
- **Overlay network (WireGuard/Tailscale) as the primary** — viable; folded in as the
  multi-device alternative to SSH rather than the default.
- **Always-on VPN** — heavier than SSH for the common single-machine LAN case.
- **No auth, trust the LAN** — rejected: LAN neighbors are part of the threat model.

## Related

- ADR-0018 (local UDS + cross-host bearer baseline this builds on),
  [ADR-0019](0019-job-scheduling.md) (per-requester queue; principal owns its jobs),
  [ADR-0021](0021-internet-cloud-worker.md) (the deferred internet tier),
  ADR-0007 (traefik TLS termination; supervision), ADR-0002 (the desktop↔cube topology),
  ADR-0017 (the Rust client that holds the per-endpoint config + principal).
- Forthcoming: the **live audio-ingest** path (capture daemon) inherits these tiers.
