"""Tests for the streaming subprocess helper + progress renderers."""

from __future__ import annotations

import sys

from transcribbler.cores.proc import run_streamed
from transcribbler.cores.pyannote import _parse_progress as diar_parse
from transcribbler.cores.whisper_cpp import _parse_progress as asr_parse
from transcribbler.progress import stderr_sink


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


def test_on_line_exception_degrades_to_passthrough(capsys):
    # A renderer that raises must not stall the stderr drain (would deadlock the
    # child on a full pipe); it falls back to writing the raw line.
    def boom(_ln):
        raise ValueError("renderer bug")

    rc, _out, _tail = run_streamed(
        [sys.executable, "-c", "import sys; sys.stderr.write('hello\\n')"],
        stream=True,
        on_line=boom,
    )
    assert rc == 0
    assert "hello" in capsys.readouterr().err


def test_dangling_progress_line_gets_closed(capsys):
    # A `\r`-style progress write with no trailing newline is closed off so the
    # next output isn't glued onto it.
    run_streamed(
        [sys.executable, "-c", "import sys; sys.stderr.write('x\\n')"],
        stream=True,
        on_line=lambda ln: "\r  ASR  50%",  # never newline-terminated
    )
    assert capsys.readouterr().err.endswith("\n")


def test_asr_progress_parses_and_renders():
    sink = stderr_sink()
    ev = asr_parse("whisper_print_progress_callback: progress =  10%")
    assert ev is not None and ev.stage == "asr" and ev.pct == 10
    assert sink(ev) == "\r  ASR  10%"
    assert sink(ev) is None  # deduped
    assert asr_parse("some unrelated line") is None
    assert sink(asr_parse("progress = 100%")).endswith("\n")  # newline on completion


def test_diar_progress_parses_and_renders_step_change():
    sink = stderr_sink()
    ev = diar_parse("@@P@@\tsegmentation\t2\t10\n")
    assert ev is not None and ev.stage == "diar" and ev.step == "segmentation" and ev.pct == 20
    first = sink(ev)
    assert "segmentation" in first and "20%" in first
    assert sink(ev) is None  # deduped
    # a new step starts with a newline so the finished step stays visible
    nxt = sink(diar_parse("@@P@@\tembeddings\t5\t10\n"))
    assert nxt.startswith("\n") and "embeddings" in nxt
    assert diar_parse("not a sentinel line") is None
