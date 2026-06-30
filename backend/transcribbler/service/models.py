"""Wire models + version constants for the ADR-0018 contract.

Pydantic models shape the JSON responses (and the OpenAPI schema FastAPI derives);
the SSE event *payloads* are plain dicts built in ``jobs.py`` so the event stream
stays close to the lifecycle.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..ir import SCHEMA_VERSION

# Bumped when the endpoints/events change incompatibly; reported by /v1/version
# alongside the IR schema version so a client and backend deployed independently
# (ADR-0002) detect drift. The major also lives in the path (/v1).
WIRE_VERSION = "1"
IR_SCHEMA_VERSION = SCHEMA_VERSION


class ErrorInfo(BaseModel):
    code: str  # oom | wont_fit | auth | bad_input | internal (ADR-0018)
    message: str


class JobCreated(BaseModel):
    id: str
    status: str


class JobState(BaseModel):
    id: str
    status: str
    profile: str
    diarize: bool
    canon: bool
    error: ErrorInfo | None = None
    ir: dict | None = None  # populated only when status == "done"


class ProfileInfo(BaseModel):
    name: str
    asr: str | None = None
    diar: str | None = None
    llm: str | None = None


class VersionInfo(BaseModel):
    wire_version: str
    ir_schema_version: str
    capabilities: dict
