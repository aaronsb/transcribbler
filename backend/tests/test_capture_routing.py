"""Graph-follow source resolution (``capture._routed_sources``).

The resolver reads the live PipeWire object graph and follows the meeting app's
*active* links to its capturable sources — the fix for detection stranding capture
on a silent default-sink monitor. It takes the parsed dump as an argument, so these
tests exercise the real logic against synthetic graphs, no PipeWire required.
"""

from __future__ import annotations

import pytest

from transcribbler.capture import SourceError, _capture_filter, _routed_sources


def _node(nid: int, media_class: str, node_name: str, app: str | None = None) -> dict:
    props = {"media.class": media_class, "node.name": node_name}
    if app is not None:
        props["application.name"] = app
    return {"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def _link(out_id: int, in_id: int) -> dict:
    return {
        "type": "PipeWire:Interface:Link",
        "info": {"output-node-id": out_id, "input-node-id": in_id},
    }


def _graph(*objs: dict) -> list[dict]:
    return list(objs)


def test_follows_output_to_a_sink_monitor():
    dump = _graph(
        _node(10, "Stream/Output/Audio", "Google Chrome", "Google Chrome"),
        _node(20, "Audio/Sink", "speaker_sink"),
        _link(10, 20),
    )
    mic, meeting = _routed_sources("Google Chrome", dump)
    assert meeting == ["speaker_sink.monitor"]
    assert mic is None


def test_mixes_every_sink_the_app_is_routed_into():
    # The app splits across two sinks (an effects sink + a real device); both are
    # returned so the caller can mix them, since which carries audio drifts.
    dump = _graph(
        _node(10, "Stream/Output/Audio", "Google Chrome", "Google Chrome"),
        _node(20, "Audio/Sink", "effects_sink"),
        _node(21, "Audio/Sink", "device_sink"),
        _link(10, 20),
        _link(10, 21),
    )
    _, meeting = _routed_sources("Google Chrome", dump)
    assert meeting == ["effects_sink.monitor", "device_sink.monitor"]


def test_resolves_mic_from_the_apps_input_link():
    # A capture stream named "Google Chrome input" reads from a source; loose matching
    # ("Google Chrome" ⊂ "Google Chrome input") finds it, and the source it reads from
    # is the mic.
    dump = _graph(
        _node(30, "Stream/Input/Audio", "Google Chrome input", "Google Chrome input"),
        _node(40, "Audio/Source", "echo_cancel_source"),
        _link(40, 30),
    )
    mic, meeting = _routed_sources("Google Chrome", dump)
    assert mic == "echo_cancel_source"
    assert meeting == []


def test_no_routing_yields_empty_meeting():
    # The app exists but isn't linked to any sink (nothing playing yet). Empty is the
    # honest answer — the caller reports it rather than falling back to a dead default.
    dump = _graph(
        _node(10, "Stream/Output/Audio", "Google Chrome", "Google Chrome"),
        _node(20, "Audio/Sink", "speaker_sink"),
    )
    mic, meeting = _routed_sources("Google Chrome", dump)
    assert meeting == []
    assert mic is None


def test_other_apps_are_ignored():
    # Spotify playing into a sink must not be mistaken for the meeting.
    dump = _graph(
        _node(10, "Stream/Output/Audio", "Spotify", "Spotify"),
        _node(20, "Audio/Sink", "speaker_sink"),
        _link(10, 20),
    )
    _, meeting = _routed_sources("Google Chrome", dump)
    assert meeting == []


def test_duplicate_routes_are_deduped():
    dump = _graph(
        _node(10, "Stream/Output/Audio", "Google Chrome", "Google Chrome"),
        _node(11, "Stream/Output/Audio", "Google Chrome", "Google Chrome"),  # 2nd stream
        _node(20, "Audio/Sink", "speaker_sink"),
        _link(10, 20),
        _link(11, 20),  # both streams into the same sink
    )
    _, meeting = _routed_sources("Google Chrome", dump)
    assert meeting == ["speaker_sink.monitor"]


def test_malformed_dump_does_not_raise():
    # A node missing its id, and junk objects — parsing must stay defensive.
    dump = [
        {"type": "PipeWire:Interface:Node", "info": {"props": {"media.class": "Audio/Sink"}}},  # no id
        {"type": "PipeWire:Interface:Link", "info": {}},  # no node ids
        {"nonsense": True},
    ]
    mic, meeting = _routed_sources("Google Chrome", dump)
    assert mic is None and meeting == []


# ---- _capture_filter: the ffmpeg -filter_complex the segmenter runs ----------


def test_filter_single_meeting_source_no_amix():
    # One monitor → straight pan to [s], no amix; join mic(L)+meeting(R).
    fc = _capture_filter(1)
    assert "amix" not in fc
    assert "[1:a]aresample=16000,pan=mono|c0=c0[s]" in fc
    assert fc.endswith("[m][s]join=inputs=2:channel_layout=stereo[o]")


def test_filter_multi_meeting_sources_sum_via_amix():
    # Three monitors → each panned to [s1..s3], summed (normalize=0), joined.
    fc = _capture_filter(3)
    for i in (1, 2, 3):
        assert f"[{i}:a]aresample=16000,pan=mono|c0=c0[s{i}]" in fc
    assert "[s1][s2][s3]amix=inputs=3:normalize=0[s]" in fc
    assert fc.endswith("[m][s]join=inputs=2:channel_layout=stereo[o]")


def test_filter_rejects_zero_sources():
    # Guarded upstream by run_capture; belt-and-suspenders so a bad call fails loudly
    # rather than emitting a malformed graph.
    with pytest.raises(SourceError):
        _capture_filter(0)
