VENV := .venv
PY := $(VENV)/bin/python

.PHONY: validate-schemas venv clean-venv

# Create/refresh an isolated venv with the tooling deps (reproducible on Arch/PEP 668 + CI).
venv:
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -r requirements.txt

# Validate JSON schemas and their fixtures (schemas/README.md).
validate-schemas: venv
	$(PY) schemas/validate.py

clean-venv:
	rm -rf $(VENV)

# End-to-end backend smoke test: transcribe the whisper.cpp JFK sample (if present)
# through the desktop-vulkan profile and validate the IR. Skips cleanly if absent.
SAMPLE := $(HOME)/Projects/ai/whisper.cpp/samples/jfk.wav
.PHONY: backend-smoke
backend-smoke:
	@test -f "$(SAMPLE)" || { echo "skip: $(SAMPLE) not found (build whisper.cpp first)"; exit 0; }
	uv run --project backend transcribbler transcribe "$(SAMPLE)" -p profiles/desktop-vulkan.toml --no-diarize -o /tmp/transcribbler-smoke.ir.json
	@echo "smoke IR written to /tmp/transcribbler-smoke.ir.json (ASR-only; diarization needs the gated model)"

# Backend unit tests (alignment, IR construction).
.PHONY: backend-test
backend-test:
	uv run --project backend --group dev pytest backend/tests -q
