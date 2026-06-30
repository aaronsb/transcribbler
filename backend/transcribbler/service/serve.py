"""Run the service (ADR-0018): ``transcribbler-serve``.

Binds the *same* ASGI app to either a Unix socket (default, local, no auth — the
OS enforces access via the per-user runtime dir) or a TCP host/port (remote).

TCP is exposed here for development only: bearer-token + TLS (ADR-0020) is not
implemented yet, so a TCP bind prints a loud warning and must stay on a trusted
network behind a terminating proxy. The transport is chosen by flags, nothing
else differs.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import env
from .app import create_app


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(os.environ.get("TMPDIR", "/tmp"))
    return base / "transcribbler.sock"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcribbler-serve", description="transcribbler HTTP service (ADR-0018)")
    parser.add_argument("--uds", help=f"bind a Unix socket (default: {default_socket_path()})")
    parser.add_argument("--host", help="bind a TCP host (remote; dev-only, no auth yet)")
    parser.add_argument("--port", type=int, default=8080, help="TCP port (with --host; default: 8080)")
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print("error: uvicorn is not installed (uv sync --project backend)", file=sys.stderr)
        return 1

    env.load_env_file()  # make HF_TOKEN etc. available to the diarizer
    app = create_app()

    if args.host:
        print(
            f"WARNING: TCP bind {args.host}:{args.port} has NO auth yet (ADR-0020 pending). "
            "Use only on a trusted network behind a terminating proxy.",
            file=sys.stderr,
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    sock = Path(args.uds) if args.uds else default_socket_path()
    if len(str(sock)) > 104:  # AF_UNIX sun_path limit (~108); fail clearly, not with a bind traceback
        print(f"error: socket path too long ({len(str(sock))} chars): {sock}", file=sys.stderr)
        return 1
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()  # stale socket from a previous run
    print(f"transcribbler-serve listening on unix:{sock}", file=sys.stderr)
    uvicorn.run(app, uds=str(sock))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
