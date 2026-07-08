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

CLIENT        := clients/cli/Cargo.toml

# uv-tool package name (backend/pyproject.toml [project].name) + the systemd --user unit.
TOOL          := transcribbler-backend
UNIT          := transcribbler.service
UNIT_SRC      := packaging/$(UNIT)
UNIT_DST      := $(HOME)/.config/systemd/user

.PHONY: help install uninstall reinstall status update build test check lint format \
        validate-schemas backend-smoke clean venv client client-check dist \
        service-install service-enable service-disable service-stop service-status \
        service-logs service-uninstall

help: ## List available targets
	@awk 'BEGIN{FS=":.*##"} /^[a-zA-Z0-9_-]+:.*##/{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

## --- setup (lifecycle) ---

install: ## Install `transcribbler` + `transcribbler-serve` to ~/.local/bin (uv tool, editable) + diarizer venv
	uv tool install --editable ./backend --force
	uv sync --project backend/diarizer
	@echo "installed: transcribbler + transcribbler-serve -> $(BIN) (uv tool, editable)"
	@echo "  editable: the diarizer sidecar (uv run --project backend/diarizer) and .env.hf resolve"
	@echo "  relative to this repo, so keep the working tree in place. Move it? re-run 'make install'."
	@case ":$$PATH:" in *":$(BIN):"*) ;; *) echo "NOTE: $(BIN) is not on PATH — add it, or run 'uv tool update-shell'";; esac

uninstall: service-uninstall ## Remove the installed CLI (and the systemd --user unit)
	uv tool uninstall $(TOOL)

reinstall: ## Reinstall the CLI in place (pick up new entrypoints / metadata)
	$(MAKE) uninstall
	$(MAKE) install

status: ## Show what's installed: CLI entrypoints, uv tool, and the service state
	@echo "== CLI =="; command -v transcribbler || echo "  transcribbler: not on PATH"
	@command -v transcribbler-serve || echo "  transcribbler-serve: not on PATH"
	@echo "== uv tool =="; uv tool list 2>/dev/null | grep -A2 '^$(TOOL)' || echo "  $(TOOL): not installed"
	@echo "== service =="; systemctl --user is-enabled $(UNIT) 2>/dev/null && systemctl --user is-active $(UNIT) 2>/dev/null || echo "  $(UNIT): not enabled"

## --- service (systemd --user, ADR-0007) ---

service-install: ## Install the systemd --user unit into ~/.config/systemd/user + reload
	@mkdir -p "$(UNIT_DST)"
	install -m 644 "$(UNIT_SRC)" "$(UNIT_DST)/$(UNIT)"
	systemctl --user daemon-reload
	@echo "installed unit: $(UNIT_DST)/$(UNIT) — enable it with 'make service-enable'"

service-enable: service-install ## Enable + start the service now and on every login
	systemctl --user enable --now $(UNIT)

service-disable: ## Stop + disable the service (keeps the unit file)
	-systemctl --user disable --now $(UNIT)

service-stop: ## Stop the service (leaves it enabled)
	-systemctl --user stop $(UNIT)

service-status: ## Show the service status
	-systemctl --user status $(UNIT) --no-pager

service-logs: ## Tail the service logs (journald)
	journalctl --user -u $(UNIT) -f

service-uninstall: ## Disable the service and remove the unit file
	-systemctl --user disable --now $(UNIT) 2>/dev/null
	@rm -f "$(UNIT_DST)/$(UNIT)" && systemctl --user daemon-reload 2>/dev/null || true
	@echo "removed unit: $(UNIT_DST)/$(UNIT)"

build: ## Build whisper.cpp (Vulkan) + fetch the model (WHISPER_DIR, WHISPER_MODEL)
	@test -d "$(WHISPER_DIR)" || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$(WHISPER_DIR)"
	cmake -S "$(WHISPER_DIR)" -B "$(WHISPER_DIR)/build" -DGGML_VULKAN=1 -DCMAKE_BUILD_TYPE=Release
	cmake --build "$(WHISPER_DIR)/build" -j
	@test -f "$(WHISPER_DIR)/models/ggml-$(WHISPER_MODEL).bin" || bash "$(WHISPER_DIR)/models/download-ggml-model.sh" $(WHISPER_MODEL)

update: ## Pull latest, re-sync deps, reinstall the CLI
	git pull --ff-only
	$(MAKE) install

## --- quality ---

test: ## Run backend unit tests
	uv run --project backend --group dev pytest backend/tests -q

lint: ## Lint the backend (ruff)
	uv run --project backend --group dev ruff check backend/transcribbler backend/tests

format: ## Auto-format the backend (ruff)
	uv run --project backend --group dev ruff format backend/transcribbler backend/tests

check: validate-schemas test lint client-check ## Run all quality gates (schemas + backend + client)

client: ## Build the Rust CLI client (clients/cli)
	cargo build --manifest-path $(CLIENT)

client-check: ## Rust client quality gates (fmt + clippy + tests)
	cargo fmt --manifest-path $(CLIENT) --check
	cargo clippy --manifest-path $(CLIENT) --quiet -- -D warnings
	cargo test --manifest-path $(CLIENT) --quiet

venv: ## Create the schema-validation venv (used by validate-schemas)
	@test -d $(VENV) || python3 -m venv $(VENV)
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -r requirements.txt

validate-schemas: venv ## Validate JSON schemas + golden/negative fixtures
	$(PY) schemas/validate.py

backend-smoke: ## End-to-end ASR smoke on the whisper.cpp sample (skips if absent)
	@test -f "$(SAMPLE)" || { echo "skip: $(SAMPLE) not found (run 'make build' first)"; exit 0; }
	uv run --project backend transcribbler transcribe "$(SAMPLE)" -p profiles/desktop-vulkan.toml --no-diarize -f md

dist: ## Build a distributable backend wheel + sdist into dist/ (uv build)
	uv build --project backend -o dist
	@echo "built: dist/ — install elsewhere with 'uv tool install <wheel>' or 'pipx install <wheel>'"

## --- housekeeping ---

clean: ## Remove Python venvs, build artifacts, and caches (keeps the whisper.cpp build)
	rm -rf $(VENV) backend/.venv backend/diarizer/.venv dist
	find backend -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
