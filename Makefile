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
