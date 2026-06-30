//! Canonical IR types — generated at build time from `schemas/canonical-ir.schema.json`
//! (see `build.rs`). The generated `CanonicalIr` and its members deserialize the
//! `ir` payload the service returns; the renderers in `render.rs` consume them.

#![allow(clippy::all, dead_code)]

include!(concat!(env!("OUT_DIR"), "/ir.rs"));
