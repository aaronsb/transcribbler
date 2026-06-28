.PHONY: validate-schemas

# Validate JSON schemas and their golden fixtures (schemas/README.md).
validate-schemas:
	python3 schemas/validate.py
