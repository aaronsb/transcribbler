//! Codegen the Canonical IR types from the shared JSON Schema (ADR-0017).
//!
//! The Rust client's IR types are generated from the *same* `schemas/` the
//! Python backend validates against, so the two can't drift silently — and the
//! `/v1/version` `ir_schema_version` handshake catches it if the schema is
//! bumped without regenerating. Output lands in `$OUT_DIR/ir.rs`, included by
//! `src/ir.rs`.

use std::{env, fs, path::PathBuf};

use typify::{TypeSpace, TypeSpaceSettings};

fn main() {
    // clients/cli/build.rs -> repo_root/schemas/canonical-ir.schema.json
    let schema_path = "../../schemas/canonical-ir.schema.json";
    println!("cargo:rerun-if-changed={schema_path}");

    let content =
        fs::read_to_string(schema_path).unwrap_or_else(|e| panic!("read {schema_path}: {e}"));
    let mut value: serde_json::Value =
        serde_json::from_str(&content).expect("parse canonical-ir schema as JSON");
    loosen_for_codegen(&mut value);
    let schema: schemars::schema::RootSchema =
        serde_json::from_value(value).expect("parse loosened canonical-ir schema");

    let mut type_space =
        TypeSpace::new(TypeSpaceSettings::default().with_derive("Clone".to_string()));
    type_space
        .add_root_schema(schema)
        .expect("typify the IR schema");

    let code = prettyplease::unparse(
        &syn::parse2::<syn::File>(type_space.to_stream()).expect("parse generated tokens"),
    );
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("ir.rs");
    fs::write(&out, code).unwrap_or_else(|e| panic!("write {}: {e}", out.display()));
}

/// Strip two validation-only constructs so codegen sees a permissive, structural
/// schema. Neither changes what the *types* are; both would otherwise hurt the
/// client:
///   - `if`/`then`/`else` — typify can't represent them (the conditionally-
///     required fields are already optional properties anyway).
///   - `additionalProperties: false` — makes typify emit `deny_unknown_fields`,
///     which would reject an *additive* (non-breaking, same `schema_version`) IR
///     field and silently break `render md|vtt` on an older client. The client
///     should be liberal in what it accepts; the backend still validates strictly.
///
/// `is_schema_map` marks objects whose *keys are user field names* (`properties`,
/// `$defs`, …) so we never mistake a field literally named `if` for the keyword.
fn loosen_for_codegen(value: &mut serde_json::Value) {
    const SCHEMA_MAPS: [&str; 4] = ["properties", "$defs", "definitions", "patternProperties"];
    fn walk(value: &mut serde_json::Value, is_schema_map: bool) {
        match value {
            serde_json::Value::Object(map) => {
                if !is_schema_map {
                    map.remove("if");
                    map.remove("then");
                    map.remove("else");
                    if map.get("additionalProperties") == Some(&serde_json::Value::Bool(false)) {
                        map.remove("additionalProperties");
                    }
                }
                for (k, v) in map.iter_mut() {
                    let child_is_map = !is_schema_map && SCHEMA_MAPS.contains(&k.as_str());
                    walk(v, child_is_map);
                }
            }
            serde_json::Value::Array(items) => {
                for v in items {
                    walk(v, false);
                }
            }
            _ => {}
        }
    }
    walk(value, false);
}
