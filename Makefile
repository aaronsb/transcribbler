# transcribbler — project control panel. `make help` lists targets.
.DEFAULT_GOAL := help

# --- config (override on the command line, e.g. `make build WHISPER_MODEL=large-v3`) ---
# NOTE: no inline comments after values — Make would fold the trailing whitespace
# into the variable (breaking paths).
# where the whisper.cpp engine is built:
WHISPER_DIR   ?= $(HOME)/Projects/ai/whisper.cpp
# ggml model to fetch:
WHISPER_MODEL ?= base.en
# where `make install` puts the launcher:
BIN           ?= $(HOME)/.local/bin
VENV          := .venv
PY            := $(VENV)/bin/python
SAMPLE        := $(WHISPER_DIR)/samples/jfk.wav

.PHONY: help install uninstall build test check lint format validate-schemas backend-smoke clean update venv

help: ## List available targets
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z0-9_-]+:.*##/{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

## --- setup ---

install: ## Install Python deps + put a `transcribbler` launcher on PATH (~/.local/bin)
	uv sync --project backend
	uv sync --project backend/diarizer
	@mkdir -p "$(BIN)"
	@printf '#!/usr/bin/env bash\nexec uv run --project "%s/backend" transcribbler "$$@"\n' "$(CURDIR)" > "$(BIN)/transcribbler"
	@chmod +x "$(BIN)/transcribbler"
	@echo "installed: $(BIN)/transcribbler -> uv run --project $(CURDIR)/backend"
	@case ":$$PATH:" in *":$(BIN):"*) ;; *) echo "NOTE: $(BIN) is not on PATH — add it to use 'transcribbler' directly";; esac

uninstall: ## Remove the installed launcher
	@rm -f "$(BIN)/transcribbler" && echo "removed $(BIN)/transcribbler"

build: ## Build whisper.cpp (Vulkan) + fetch the model (WHISPER_DIR, WHISPER_MODEL)
	@test -d "$(WHISPER_DIR)" || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$(WHISPER_DIR)"
	cmake -S "$(WHISPER_DIR)" -B "$(WHISPER_DIR)/build" -DGGML_VULKAN=1 -DCMAKE_BUILD_TYPE=Release
	cmake --build "$(WHISPER_DIR)/build" -j
	@test -f "$(WHISPER_DIR)/models/ggml-$(WHISPER_MODEL).bin" || bash "$(WHISPER_DIR)/models/download-ggml-model.sh" $(WHISPER_MODEL)

update: ## Pull latest, re-sync deps, refresh the launcher
	git pull --ff-only
	$(MAKE) install

## --- quality ---

test: ## Run backend unit tests
	uv run --project backend --group dev pytest backend/tests -q

lint: ## Lint the backend (ruff)
	uv run --project backend --group dev ruff check backend/transcribbler backend/tests

format: ## Auto-format the backend (ruff)
	uv run --project backend --group dev ruff format backend/transcribbler backend/tests

check: validate-schemas test lint ## Run all quality gates (schemas + tests + lint)

venv: ## Create the schema-validation venv (used by validate-schemas)
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -r requirements.txt

validate-schemas: venv ## Validate JSON schemas + golden/negative fixtures
	$(PY) schemas/validate.py

backend-smoke: ## End-to-end ASR smoke on the whisper.cpp sample (skips if absent)
	@test -f "$(SAMPLE)" || { echo "skip: $(SAMPLE) not found (run 'make build' first)"; exit 0; }
	uv run --project backend transcribbler transcribe "$(SAMPLE)" -p profiles/desktop-vulkan.toml --no-diarize -f md

## --- housekeeping ---

clean: ## Remove Python venvs and caches (keeps the whisper.cpp build)
	rm -rf $(VENV) backend/.venv backend/diarizer/.venv
	find backend -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
