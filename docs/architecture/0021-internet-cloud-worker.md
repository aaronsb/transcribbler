# ADR-0021: Internet access via an authenticated cloud worker (deferred)

- **Status**: Proposed
- **Date**: 2026-06-29
- **Deciders**: Aaron

## Context

[ADR-0020](0020-remote-access-security.md) tiers remote access by locality and
covers the near-term paths (local UDS, LAN over SSH, advanced direct-TCP). The
*internet / away-from-home* tier is a different animal — it is the one case where
reaching your own GPU is awkward (home uplink, NAT, cube uptime) and where you may
prefer to **not** punch a hole home at all. It is also clearly **future work**, so
it is recorded here as a standalone proposed decision rather than holding up the
near-term security model.

The shape (from design discussion): rather than expose the home backend to the
internet, an **authenticated edge** answers, and the actual compute happens in the
**cloud**. This is the project's *"cloud fallback is optional, never required"*
posture (Goal #1) made concrete — and it only works because every backend, cloud
included, converges on the same Canonical IR ([ADR-0006](0006-canonical-ir-contract.md),
[ADR-0015](0015-pluggable-compute-backends.md)'s swap seam).

## Decision (proposed; not yet built)

When away from any trusted network, the client talks to an **authenticated
Cloudflare Worker**, which calls **cloud AI providers** and maps their output to the
Canonical IR:

- **Edge auth at the worker** (Cloudflare Access / token) — the public entry point is
  the worker, never the home origin.
- **Provider API keys live in the worker's secrets**, never on the client.
- **Diarization-capable providers** (e.g. Deepgram / AssemblyAI; plain OpenAI Whisper
  is ASR-only) so the cloud path can produce the *same* speaker-attributed IR; the
  worker owns the provider→IR mapping.
- **The home GPU is never reached** — this path is cloud compute, decoupling "remote
  access" from "GPU exposure."
- **Explicit opt-in + consent.** Audio leaves your machines to a third party, so this
  is never a silent fallback from a failed local/LAN attempt; it is a deliberate
  endpoint selection and a consent event ([ADR-0013](0013-retention-and-consent.md)).

The client sees this as just another **named endpoint** (worker URL + credential);
no client code path differs beyond the connector and the consent gate.

## Consequences

### Good

- Internet reach with **no public hole into the home network** and no dependency on
  home uplink/cube uptime.
- Reuses the IR contract — cloud is "just another backend," client stays uniform.
- Credentials for paid providers stay server-side at the edge.

### Bad / costs

- **Data leaves your machines** — weakens local-first for this path; acceptable only
  as consented opt-in.
- Per-provider→IR mapping to build and maintain; provider features (diarization
  quality, timestamps) vary.
- A Cloudflare Worker is a second deployment artifact and a paid-provider cost center.

## Alternatives considered

- **Private tunnel to the home GPU** (Cloudflare Tunnel / `cloudflared`, or Tailscale
  to cube) — preserves local-first (compute stays home, no open port). Viable and a
  likely future companion; not chosen as the away-default because cloud providers were
  preferred for convenience when home may be down. Recorded so it isn't lost.
- **Expose the backend directly with login/OAuth** — rejected (see ADR-0020): wrong
  shape and a permanent public attack surface.

## Related

- ADR-0020 (the tiered access model this is the internet tier of), ADR-0013 (consent
  for data leaving), ADR-0015 + ADR-0006 (cloud as another IR-emitting backend),
  ADR-0002 (the topology), ADR-0017 (client holds the per-endpoint config).
