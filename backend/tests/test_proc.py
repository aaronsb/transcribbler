"""Tests for the streaming subprocess helper + progress renderers."""

from __future__ import annotations

import sys

from transcribbler.cores.proc import run_streamed
from transcribbler.cores.pyannote import _progress_renderer as diar_renderer
from transcribbler.cores.whisper_cpp import _progress_renderer as asr_renderer


def test_captures_stdout_whole():
    rc, out, tail = run_streamed(
        [sys.executable, "-c", "print('hello'); print('world')"], stream=False
    )
    assert rc == 0
    assert out == "hello\nworld\n"
    assert tail == ""


def test_keeps_stderr_tail_for_errors():
    rc, _out, tail = run_streamed(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
        stream=False,
    )
    assert rc == 3
    assert "boom" in tail


def test_on_line_filters_what_streams(capsys):
    # stream=True with an on_line that only echoes lines containing "keep"
    run_streamed(
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('drop me\\nkeep me\\n')",
        ],
        stream=True,
        on_line=lambda ln: ln if "keep" in ln else None,
    )
    captured = capsys.readouterr()
    assert "keep me" in captured.err
    assert "drop me" not in captured.err


def test_asr_renderer_parses_and_dedupes():
    render = asr_renderer()
    assert render("whisper_print_progress_callback: progress =  10%") == "\r  ASR  10%"
    assert render("whisper_print_progress_callback: progress =  10%") is None  # deduped
    assert render("some unrelated line") is None
    assert render("progress = 100%").endswith("\n")  # newline on completion


def test_diar_renderer_handles_sentinel_and_step_change():
    render = diar_renderer()
    first = render("@@P@@\tsegmentation\t2\t10\n")
    assert "segmentation" in first and "20%" in first
    assert render("@@P@@\tsegmentation\t2\t10\n") is None  # deduped
    # a new step starts with a newline so the finished step stays visible
    nxt = render("@@P@@\tembeddings\t5\t10\n")
    assert nxt.startswith("\n") and "embeddings" in nxt
    assert render("not a sentinel line") is None
