#!/usr/bin/env python3
"""Validate transcribbler schemas and their fixtures.

Checks:
  1. each schema is a valid JSON Schema draft 2020-12;
  2. every fixture in examples/ validates against its schema (with format checking);
  3. every fixture in examples/invalid/ is REJECTED by its schema (locks in "has teeth");
  4. no fixture file is orphaned — every file under examples/ must be referenced here.

Exit non-zero on any failure so this can gate CI. Deps: jsonschema, rfc3339-validator
(for date-time format checking); see requirements.txt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).parent
EXAMPLES = HERE / "examples"

# schema file -> fixtures that MUST validate
VALID_CASES = {
    "canonical-ir.schema.json": [
        "examples/session-modular.ir.json",
        "examples/batch-joint.ir.json",
        "examples/fallback-speakers.ir.json",
    ],
    "canonicalization-data.schema.json": [
        "examples/canonicalization-data.example.json",
    ],
}

# schema file -> fixtures that MUST be rejected (negative tests)
INVALID_CASES = {
    "canonical-ir.schema.json": [
        "examples/invalid/turn-missing-speaker-id.json",
        "examples/invalid/bad-speaker-id.json",
        "examples/invalid/unknown-field.json",
        "examples/invalid/batch-missing-sha256.json",
        "examples/invalid/glossary-empty-variants.json",
        "examples/invalid/bad-captured-at.json",
    ],
    "canonicalization-data.schema.json": [
        "examples/invalid/canon-data-empty-variants.json",
    ],
}


def load(rel: str) -> dict:
    return json.loads((HERE / rel).read_text())


def validator_for(schema_file: str) -> Draft202012Validator:
    schema = load(schema_file)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def main() -> int:
    failures: list[str] = []

    # 4. orphan detection — every fixture on disk must be declared above.
    declared = {
        f
        for groups in (VALID_CASES, INVALID_CASES)
        for fixtures in groups.values()
        for f in fixtures
    }
    on_disk = {
        str(p.relative_to(HERE)) for p in EXAMPLES.rglob("*.json")
    }
    for orphan in sorted(on_disk - declared):
        failures.append(f"{orphan}: fixture on disk is not referenced in validate.py")
    for missing in sorted(declared - on_disk):
        failures.append(f"{missing}: referenced fixture does not exist on disk")

    for schema_file, fixtures in VALID_CASES.items():
        try:
            validator = validator_for(schema_file)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{schema_file}: invalid schema: {e}")
            continue
        print(f"ok   schema {schema_file}")
        for fx in fixtures:
            try:
                doc = load(fx)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{fx}: could not load: {e}")
                continue
            errors = sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.path)))
            if errors:
                for err in errors:
                    loc = "/".join(map(str, err.path)) or "<root>"
                    failures.append(f"{fx} @ {loc}: {err.message}")
            else:
                print(f"ok   valid   {fx}")

    for schema_file, fixtures in INVALID_CASES.items():
        try:
            validator = validator_for(schema_file)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{schema_file}: invalid schema: {e}")
            continue
        for fx in fixtures:
            try:
                doc = load(fx)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{fx}: could not load: {e}")
                continue
            if any(validator.iter_errors(doc)):
                print(f"ok   reject  {fx}")
            else:
                failures.append(f"{fx}: expected REJECTION but schema accepted it")

    if failures:
        print("\nFAILURES:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("\nall schemas + fixtures valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
