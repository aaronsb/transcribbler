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
    // typify can't represent if/then/else (they're validation-only — they don't
    // shape the Rust types, since the conditionally-required fields are already
    // optional properties). Scrub them so codegen sees a pure structural schema.
    let mut value: serde_json::Value =
        serde_json::from_str(&content).expect("parse canonical-ir schema as JSON");
    strip_conditionals(&mut value);
    let schema: schemars::schema::RootSchema =
        serde_json::from_value(value).expect("parse scrubbed canonical-ir schema");

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

/// Recursively drop `if`/`then`/`else` keys (JSON Schema conditionals) anywhere
/// in the document. They constrain validation, not structure, so removing them
/// leaves the generated types unchanged.
fn strip_conditionals(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::Object(map) => {
            map.remove("if");
            map.remove("then");
            map.remove("else");
            for v in map.values_mut() {
                strip_conditionals(v);
            }
        }
        serde_json::Value::Array(items) => {
            for v in items {
                strip_conditionals(v);
            }
        }
        _ => {}
    }
}
