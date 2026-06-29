"""llama.cpp canonicalizer core (ADR-0004/0006).

Calls a llama-server (OpenAI-compatible) to produce canonicalization data, with:
- a hand-written GBNF grammar (guaranteed-valid JSON; the schema's regex pattern
  breaks llama.cpp's built-in converter),
- non-thinking mode (chat_template_kwargs.enable_thinking=false) — this is structured
  extraction, not reasoning,
- temperature 0 + fixed seed for reproducibility.

If the profile points at an already-running server (options.endpoint) it is reused;
otherwise a server is spawned for the call and shut down after (frees VRAM, ADR-0011).
The returned object is schema-validated before it leaves this core.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from jsonschema import Draft202012Validator

from ..canon import SYSTEM_PROMPT
from ..profiles import StageConfig

_REPO = Path(__file__).resolve().parents[3]
_GRAMMAR = _REPO / "schemas" / "canonicalization-data.gbnf"
_SCHEMA = _REPO / "schemas" / "canonicalization-data.schema.json"
_SPAWN_TIMEOUT_S = 180


class LlamaCanonicalizer:
    name = "llama.cpp"

    def __init__(self, cfg: StageConfig):
        self.binary = cfg.binary
        self.model = cfg.model
        self.endpoint = cfg.options.get("endpoint")
        self.port = int(cfg.options.get("port", 8099))
        self.grammar = _GRAMMAR.read_text()
        self._validator = Draft202012Validator(json.loads(_SCHEMA.read_text()))

    def canonicalize(self, evidence: str) -> dict:
        with self._server() as base_url:
            return self._request(base_url, evidence)

    @contextmanager
    def _server(self):
        if self.endpoint and _healthy(self.endpoint):
            yield self.endpoint.rstrip("/")
            return
        if not self.binary or not Path(self.binary).exists():
            raise FileNotFoundError(f"llama-server binary not found: {self.binary}")
        if not self.model or not Path(self.model).exists():
            raise FileNotFoundError(f"canonicalization model not found: {self.model}")
        base = f"http://127.0.0.1:{self.port}"
        proc = subprocess.Popen(
            [self.binary, "-m", self.model, "-ngl", "99", "--host", "127.0.0.1",
             "--port", str(self.port), "--jinja", "-c", "8192", "--temp", "0"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            _wait_healthy(base, _SPAWN_TIMEOUT_S)
            yield base
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _request(self, base_url: str, evidence: str) -> dict:
        payload = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": evidence},
            ],
            "temperature": 0,
            "seed": 1,
            "max_tokens": 1024,
            "grammar": self.grammar,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
        content = resp["choices"][0]["message"]["content"]
        data = json.loads(content)
        errors = sorted(self._validator.iter_errors(data), key=lambda e: list(map(str, e.path)))
        if errors:
            raise ValueError(f"canonicalization output failed schema: {errors[0].message}")
        return data


def _healthy(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=3) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _wait_healthy(base_url: str, timeout_s: int) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _healthy(base_url):
            return
        time.sleep(1.0)
    raise RuntimeError(f"llama-server did not become healthy within {timeout_s}s")
