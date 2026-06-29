"""Subprocess helper that streams a child's stderr as live progress.

`subprocess.run()` buffers, so a long ASR/diarize run looks frozen for minutes.
`run_streamed` uses `Popen` and tees the child's stderr to ours line-by-line (so
progress shows live), while keeping a bounded tail for error messages and
capturing stdout whole (cores parse it).
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable


def run_streamed(
    cmd: list[str],
    *,
    stream: bool,
    env: dict | None = None,
    on_line: Callable[[str], str | None] | None = None,
    tail: int = 40,
) -> tuple[int, str, str]:
    """Run ``cmd``; return ``(returncode, stdout, stderr_tail)``.

    stdout is captured whole. Each stderr line is kept in a bounded tail (for
    error reporting) and, when ``stream`` is set, rendered to our stderr via
    ``on_line`` (or passed through verbatim if ``on_line`` is None). ``on_line``
    returns the text to write — it owns its own ``\\n``/``\\r`` — or None to
    suppress that line. stdout and stderr are drained concurrently (separate
    thread) so neither pipe can deadlock.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    tail_buf: deque[str] = deque(maxlen=tail)

    def pump() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail_buf.append(line)
            if not stream:
                continue
            shown = line if on_line is None else on_line(line)
            if shown:
                sys.stderr.write(shown)
                sys.stderr.flush()

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()
    assert proc.stdout is not None
    out = proc.stdout.read()
    proc.wait()
    pump_thread.join()
    return proc.returncode, out, "".join(tail_buf)
