#!/usr/bin/env python3
"""Validate transcribbler schemas and their golden fixtures.

Checks (1) each schema is valid JSON Schema draft 2020-12, and (2) every fixture
in examples/ validates against its schema. Exit non-zero on any failure so this
can gate CI. Dependency: jsonschema.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).parent

# schema file -> list of example fixtures that must validate against it
CASES = {
    "canonical-ir.schema.json": ["examples/session-modular.ir.json"],
    "canonicalization-data.schema.json": ["examples/canonicalization-data.example.json"],
}


def load(rel: str) -> dict:
    return json.loads((HERE / rel).read_text())


def main() -> int:
    failures: list[str] = []

    for schema_file, fixtures in CASES.items():
        schema = load(schema_file)
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{schema_file}: invalid schema: {e}")
            continue
        validator = Draft202012Validator(schema)
        print(f"ok  schema {schema_file}")

        for fx in fixtures:
            errors = sorted(validator.iter_errors(load(fx)), key=lambda e: e.path)
            if errors:
                for err in errors:
                    loc = "/".join(map(str, err.path)) or "<root>"
                    failures.append(f"{fx} @ {loc}: {err.message}")
            else:
                print(f"ok  fixture {fx}")

    if failures:
        print("\nFAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nall schemas + fixtures valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
