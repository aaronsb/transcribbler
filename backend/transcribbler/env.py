"""Minimal .env loader for secrets like the HF token.

Loads `KEY=VALUE` lines from a dotenv file into os.environ (without overwriting
values already set). Kept tiny and dependency-free; the file itself is gitignored.
"""

from __future__ import annotations

import os
from pathlib import Path

# repo_root/.env.hf  (backend/transcribbler/env.py -> up 3)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env.hf"


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> None:
    """Load KEY=VALUE pairs into os.environ. No-op if the file is absent."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
