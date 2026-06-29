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

| Tier | Path | Transport + identity | Compute |
|---|---|---|---|
| **Local** (same host) | UDS | OS file perms (ADR-0018) | local GPU |
| **LAN** (same room / trusted machine) | **SSH tunnel** (`ssh -L`) to the backend's loopback/UDS | **SSH keys** (already provisioned to cube) | local GPU |
| **Internet** (away) | **authed Cloudflare Worker** | edge auth (Cloudflare Access / token); provider keys in **worker secrets**, never on the client | **cloud AI providers** |
| **Direct** (advanced/opt-in) | TCP to the backend | **bearer token over TLS**, **mTLS opt-in** | local GPU |

- **The backend binds loopback / UDS by default — never `0.0.0.0`.** Reaching it from
  another machine is done by *tunneling* (SSH), not by exposing it. This makes "no
  public attack surface" the default, not a configuration the user must remember.
- **LAN = SSH.** Transport and identity in one mechanism that already exists; the
  client connects to a locally-forwarded socket. No new auth system, no open port.
  (An overlay network — Tailscale/WireGuard — is the documented alternative when many
  devices need to roam without per-hop `ssh -L`.)
- **Internet = an authed edge, not an exposed origin.** A Cloudflare Worker is the
  public, authenticated entry point; it calls **diarization-capable cloud providers**
  (e.g. Deepgram / AssemblyAI; plain OpenAI Whisper does ASR only) and **maps their
  output to the Canonical IR**, so the client sees the same contract. The home GPU is
  never reached. Provider API keys live in the worker's secrets.
  - **This path sends audio to a third party.** It is therefore **explicit opt-in**,
    surfaced as a **consent event** (ADR-0013), and **never a silent fallback** from a
    failed local/LAN attempt.
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

## Consequences

### Good

- The default posture is **zero public attack surface** — the backend isn't reachable
  off-box without an explicit tunnel.
- Each tier **reuses existing trust** (OS perms, SSH keys, Cloudflare Access) instead
  of a bespoke auth stack — less to build, less to get wrong.
- The cloud path **decouples remote access from GPU exposure**: being on the internet
  never means punching a hole home.
- "Cloud is just another IR-emitting backend" keeps the client uniform across tiers.

### Bad / costs

- The **cloud tier leaks data to a third party** by nature — acceptable only as
  opt-in with consent, and it weakens the local-first guarantee for that path.
- The cloud tier adds a **provider→IR mapping** to maintain per provider, and a
  Cloudflare Worker to operate (a second small deployment artifact).
- SSH tunneling is slightly manual per device (mitigated by client endpoint config /
  `ssh -L` automation, or an overlay network).
- mTLS, if adopted, brings a private CA and per-device cert provisioning.

## Alternatives considered

- **Expose the backend directly to the internet with a login / OAuth** — rejected:
  wrong shape (M2M, not user accounts), and a permanent public attack surface for a
  personal tool.
- **Private tunnel to the home GPU for the internet tier** (Cloudflare Tunnel /
  `cloudflared`, or Tailscale to cube) — *considered and viable*; it preserves
  local-first (compute stays home, no open port). Not the default chosen here (cloud
  providers were preferred for the away case), but documented as the privacy-preserving
  alternative and a likely future option.
- **Overlay network (WireGuard/Tailscale) as the primary** — viable; folded in as the
  multi-device alternative to SSH rather than the default.
- **Always-on VPN** — heavier than SSH for the common single-machine LAN case.
- **No auth, trust the LAN** — rejected: LAN neighbors are part of the threat model.

## Related

- ADR-0018 (local UDS + cross-host bearer baseline this builds on), ADR-0013 (consent
  for the data-leaves cloud tier; deletion), ADR-0015 + ADR-0006 (cloud as just
  another IR-emitting backend), ADR-0007 (traefik TLS termination; supervision),
  ADR-0002 (the desktop↔cube topology being secured), ADR-0017 (the Rust client that
  holds the per-endpoint config).
- Forthcoming: the **live audio-ingest** path (capture daemon) inherits these tiers.
